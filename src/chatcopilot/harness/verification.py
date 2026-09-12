"""Translate backend verification evidence into Harness-owned contracts."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from chatcopilot.harness.models import (
    CandidateRef, Coder, Evaluator, HarnessError, RepairHypothesis, RepairOptions,
    ProblemEvidence, RepairFeedback,
    VerificationCheck, VerificationPlan, VerificationResult,
)


def result_from_trials(receipt: dict[str, Any], target: str, candidate: CandidateRef) -> VerificationResult:
    checks = []
    for row in receipt["result"]["trials"]:
        if row["target_id"] != target:
            continue
        error = row.get("error") or {}
        outcome = row["outcome"]
        failure_kind = "product" if outcome == "failed" else ""
        if outcome == "error":
            code = error.get("code", "")
            if error.get("stage") == "scoring" or code == "judge_error":
                failure_kind = "judge"
            else:
                failure_kind = "environment"
        elif outcome not in {"passed", "failed"}:
            failure_kind = "evidence"
        checks.append(VerificationCheck(row["case_id"], row["attempt"], outcome,
                                        failure_kind, row))
    return VerificationResult(str(receipt.get("evaluation_id", "")), candidate.digest,
                              tuple(checks), (str(receipt.get("evaluation_id", "")),))


class CaseVerification:
    def __init__(self, evaluator: Evaluator, local: Any, store: Any) -> None:
        self.store = store
        self.evaluator = evaluator
        self.local_verifier = local

    def prepare(self, task: dict[str, Any], candidate: CandidateRef, coder: Coder,
                options: RepairOptions, check_cancel: Callable[[], None]
                ) -> tuple[dict[str, Any], RepairHypothesis, VerificationPlan]:
        source = task["source"]
        problem = ProblemEvidence(str(source.get("run_id") or source.get("case_instance_id") or source.get("case_ref")),
                                  source.get("kind", "evaluation"), str(source.get("revision", "")),
                                  source.get("evidence") or source.get("case_definition") or {},
                                  RepairFeedback(**source.get("feedback", {})))
        if task.get("verification_plan"):
            return (source, RepairHypothesis(**task["hypothesis"]),
                    VerificationPlan.from_payload(task["verification_plan"]))
        if not source.get("test_sha256") and (source.get("kind", "evaluation") == "evaluation" or not source.get("case_snapshot_id")):
            source = self.local_verifier.prepare(task, candidate.path, coder, options, check_cancel)
        real_agent = source.get("kind") == "evaluation" and source.get("executor") in {"agent_isolated", "agent_configured"}
        if source.get("agent_case") and source.get("kind") == "robot_task" and not source.get("case_snapshot_id"):
            prepare = getattr(self.evaluator, "prepare_agent_case")
            source = prepare(source, check_cancel)
            real_agent = True
        real_agent = real_agent or bool(source.get("case_snapshot_id"))
        repetitions = max(3, source["repetitions"]) if real_agent else source["repetitions"]
        diagnosis = source.get("diagnosis") or {}
        hypothesis = RepairHypothesis(
            str(diagnosis.get("reason") or f"依据 {problem.source_id} 调查根因"),
            str(diagnosis.get("expected_behavior") or (source.get("case_definition") or {}).get("expected_behavior") or "满足冻结 Case 的全部验收标准"),
        )
        primary = tuple(source.get("reproduction_ids") or (source["case_id"],))
        plan = VerificationPlan(primary, tuple(source["case_ids"]), tuple(source["passed_cases"]),
                                repetitions, real_agent, source.get("case_snapshot_id", ""))
        if source.get("kind", "evaluation") == "evaluation":
            source = {key: value for key, value in source.items() if key not in {"diagnosis", "preparation"}}
        return source, hypothesis, plan

    def run(self, task: dict[str, Any], candidate: CandidateRef, run_id: str,
            checks: list[str], check_cancel: Callable[[], None]) -> VerificationResult:
        source = task["source"]
        port = self.local_verifier if source.get("test_sha256") else self.evaluator
        receipt = port.run({**task, "source": {**source, "repetitions": task["verification_plan"]["repetitions"]}},
                           candidate.path, run_id, checks, check_cancel)
        if source.get("case_snapshot_id") and not source.get("conditions"):
            if not receipt.get("conditions"):
                raise HarnessError("verification_evidence", "首次 Agent 验证缺少冻结条件")
            source = {**source, "conditions": receipt["conditions"], "target_id": receipt["target_id"]}
            self.store.update(task["task_id"], source=source)
        return replace(result_from_trials(receipt, source["target_id"] or receipt["target_id"], candidate), run_id=run_id)

    def regressions(self, task: dict[str, Any], candidate: CandidateRef,
                    check_cancel: Callable[[], None], checks: list[str] | None = None) -> dict[str, Any]:
        if task["source"].get("test_sha256"):
            # The local plan already contains every collected repository unit test.
            return {"case_ids": [], "passed_cases": [], "failed_cases": []}
        value = self.local_verifier.regressions(task, candidate.path, check_cancel, checks)
        for row in value.get("rows", {}).values():
            if row["outcome"] == "error":
                raise HarnessError("verification_environment", "仓库回归发生执行环境错误")
        return value
