"""Translate backend verification evidence into Harness-owned contracts."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from chatcopilot.harness.models import (
    CandidateRef, Evaluator, HarnessError, RepairHypothesis,
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
        failure_kind = row.get("failure_kind") or ("product" if outcome == "failed" else "")
        if outcome == "error" and not row.get("failure_kind"):
            code = error.get("code", "")
            if error.get("stage") == "scoring" or code == "judge_error":
                failure_kind = "judge"
            else:
                failure_kind = "environment"
        elif outcome not in {"passed", "failed"} and not row.get("failure_kind"):
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
        self.local_verifier.store = store
        self.local_verifier.validate_case = getattr(evaluator, "validate_agent_case", None)

    def capabilities(self) -> dict[str, Any]:
        return self.evaluator.capabilities()

    def prepare(self, task: dict[str, Any], candidate: CandidateRef, output,
                proposal: dict[str, Any], check_cancel: Callable[[], None]
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
            source = self.local_verifier.prepare(task, candidate.path, output, proposal, check_cancel)
        real_agent = source.get("kind") == "evaluation" and source.get("executor") in {"agent_isolated", "agent_configured"}
        if source.get("agent_case") and source.get("kind") == "robot_task" and not source.get("case_snapshot_id"):
            prepare = getattr(self.evaluator, "prepare_agent_case")
            agent_source = prepare(source, check_cancel)
            source = {**source, "agent_source": agent_source} if source.get("test_sha256") else agent_source
            real_agent = True
        real_agent = real_agent or bool(source.get("case_snapshot_id"))
        repetitions = max(3, source["repetitions"]) if real_agent else source["repetitions"]
        diagnosis = source.get("diagnosis") or {}
        hypothesis = RepairHypothesis(
            str(diagnosis.get("reason") or f"依据 {problem.source_id} 调查根因"),
            str(diagnosis.get("expected_behavior") or (source.get("case_definition") or {}).get("expected_behavior") or "满足冻结 Case 的全部验收标准"),
        )
        primary = tuple(source.get("reproduction_ids") or (source["case_id"],))
        checks = tuple(source["case_ids"])
        repeat_map = {}
        if source.get("agent_source"):
            agent_ids = tuple(source["agent_source"]["case_ids"])
            primary = (*primary, *agent_ids)
            checks = (*checks, *agent_ids)
            repeat_map = {name: 3 if name in agent_ids else 1 for name in checks}
        coverage = {}
        declared = {row["requirement"]: row["checks"] for row in proposal["coverage"]}
        for item in (task.get("acceptance") or {}).get("items", []):
            agent_ids = source.get("agent_source", {}).get("case_ids") or ([] if source.get("test_sha256") else list(primary))
            covered = declared.get(item["id"], [])
            available = list(agent_ids if item["verification"] == "agent" else source.get("reproduction_ids", primary))
            node_ids = source.get("test_nodeids", {})
            resolved = []
            for name in covered:
                if name in available:
                    resolved.append(name)
                else:
                    matches = [ident for node, ident in node_ids.items()
                               if node.rsplit("::", 1)[-1].split("[", 1)[0] == name and ident in available]
                    if matches:
                        resolved.extend(matches)
                    elif name == "agent_case" and real_agent:
                        resolved.extend(agent_ids)
            coverage[item["id"]] = sorted(set(resolved)) if (item["verification"] != "agent" or real_agent) else []
        plan = VerificationPlan(primary, checks, tuple(source["passed_cases"]),
                                repetitions, real_agent, source.get("case_snapshot_id", ""), repeat_map, coverage)
        if source.get("kind", "evaluation") == "evaluation":
            source = {key: value for key, value in source.items() if key not in {"diagnosis", "preparation"}}
        return source, hypothesis, plan

    def run(self, task: dict[str, Any], candidate: CandidateRef, run_id: str,
            checks: list[str], check_cancel: Callable[[], None]) -> VerificationResult:
        source = task["source"]
        if source.get("agent_source"):
            agent_source = source["agent_source"]
            agent_ids = [name for name in checks if name in agent_source["case_ids"]]
            local_ids = [name for name in checks if name not in agent_ids]
            results = []
            if local_ids:
                receipt = self.local_verifier.run(task, candidate.path, run_id + "-local", local_ids, check_cancel)
                results.append(result_from_trials(receipt, "local-pytest", candidate))
            if agent_ids:
                receipt = self.evaluator.run({**task, "source": agent_source}, candidate.path, run_id + "-agent", agent_ids, check_cancel)
                results.append(result_from_trials(receipt, receipt["target_id"], candidate))
                if not agent_source.get("conditions"):
                    if not receipt.get("conditions"):
                        raise HarnessError("verification_evidence", "首次 Agent 验证缺少冻结条件")
                    self.store.update(task["task_id"], source={**source, "agent_source": {**agent_source,
                        "conditions": receipt["conditions"], "target_id": receipt["target_id"]}})
            return VerificationResult(run_id, candidate.digest, tuple(c for r in results for c in r.checks),
                                      tuple(ref for r in results for ref in r.evidence_refs))
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
        value = self.local_verifier.regressions(task, candidate.path, check_cancel, checks)
        for row in value.get("rows", {}).values():
            if row["outcome"] == "error":
                raise HarnessError("verification_environment", "仓库回归发生执行环境错误")
        return value
