"""GC uses the common verifier port with frozen repository gates."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.harness.models import HarnessError, VerificationCheck, VerificationPlan, VerificationResult
from chatcopilot.harness.repository_checks import RepositoryChecks, compare_verification
from chatcopilot.harness.verification import CaseVerification
from chatcopilot.harness.verification_ledger import SourceLedger


class GovernanceVerification(CaseVerification):
    def __init__(self, evaluator, local, store):
        super().__init__(evaluator, local, store)
        self._reports = {}

    def capabilities(self):
        return {"verification_kinds": ["existing", "pytest"], "purpose": "governance",
                "existing": "冻结的仓库 full 检查；仅检查通过不能单独证明治理有效"}

    def prepare(self, task, candidate, output, proposal, check_cancel):
        if task["source"].get("kind") != "code_health":
            raise HarnessError("verification_purpose", "治理验证器只处理仓库治理来源")
        if proposal["verification_kind"] == "existing":
            source = {**task["source"], "verification_kind": "existing"}
            return source, VerificationPlan(("repository_regressions",), ("repository_regressions",), (), 1,
                coverage={"expected_behavior": ["repository_regressions"]}, purpose="governance")
        if proposal["verification_kind"] != "pytest":
            raise HarnessError("test_definition", "代码治理使用仓库检查或行为保持测试；语义改变需要独立判断")
        source, plan = super().prepare(task, candidate, output, proposal, check_cancel)
        return source, replace(plan, purpose="governance")

    def run(self, task, candidate, run_id, checks, check_cancel):
        if task["source"].get("verification_kind") != "existing":
            return super().run(task, candidate, run_id, checks, check_cancel)
        if checks != ["repository_regressions"]:
            raise HarnessError("test_definition", "治理仓库验证集合已变化")
        report = self.regressions(task, candidate, check_cancel)
        return VerificationResult(run_id, candidate.digest, (
            VerificationCheck("repository_regressions", 1, "passed", evidence=report),), (report["report"],))

    def regressions(self, task, candidate, check_cancel, checks=None):
        key = task["task_id"], candidate.digest
        if key in self._reports:
            return self._reports[key]
        current = self.store.get(task["task_id"])
        directory = self.store.root / "jobs" / task["task_id"]
        frozen = directory / "source"
        baseline = current.get("regression_baseline")
        ledger = SourceLedger(frozen, current["baseline_manifest"], directory / "governance-inventory", Path(current["repository"]))
        runner = RepositoryChecks(directory, Path(current["repository"]), python=self.local_verifier.python)
        runner.bind(ledger, frozen)
        raw = runner.verify(candidate.path, "full", check_cancel)
        is_baseline = candidate.digest == manifest_digest(current["baseline_manifest"])
        retained = [] if is_baseline and all(row["exit_code"] in {0, 1} for row in raw["checks"]) else (
            compare_verification(baseline["repository_report"] if baseline else raw, raw))
        if retained is None:
            error = HarnessError("verification_environment" if any(row["exit_code"] not in {0, 1} for row in raw["checks"])
                                 else "product_failure", "治理仓库检查出现回归或缺少有效验收证据")
            error.evidence = raw
            raise error
        result = self._result(raw, retained)
        self._reports[key] = result
        # Baseline is recorded before candidate execution, including after a restart.
        if is_baseline and not baseline:
            self.store.update(task["task_id"], regression_baseline=result)
        return result

    @staticmethod
    def _result(raw, retained=()):
        rows = {"repository:" + row["name"]: {"outcome": "passed" if row["exit_code"] == 0 else "failed"}
                for row in raw["checks"]}
        return {"case_ids": sorted(rows), "rows": rows,
                  "passed_cases": sorted(name for name, row in rows.items() if row["outcome"] == "passed"),
                  "failed_cases": sorted(name for name, row in rows.items() if row["outcome"] != "passed"),
                  "retained_failures": retained, "repository_report": raw, "report": raw["report"]}

    def delivery_context(self, task_id, candidate_digest, baseline, verified):
        retained = compare_verification(baseline, verified)
        if retained is None:
            raise HarnessError("delivery_revalidation_failed", "新主干上的治理保护检查未通过")
        self._reports[task_id, candidate_digest] = self._result(verified, retained)
