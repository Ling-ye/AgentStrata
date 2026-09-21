"""Translate runtime verification evidence into Harness-owned contracts."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from chatcopilot.harness.models import (
    CandidateRef, Evaluator, HarnessError, VerificationRequest,
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

    def prepare(self, task: VerificationRequest, candidate: CandidateRef, output,
                proposal: dict[str, Any], check_cancel: Callable[[], None]
                ) -> tuple[dict[str, Any], VerificationPlan]:
        source = task["source"]
        if task.get("verification_plan"):
            return source, VerificationPlan.from_payload(task["verification_plan"])
        governance = source.get("kind") == "code_health"
        original = source if source.get("kind", "evaluation") == "evaluation" else None
        if not governance and proposal.get("verification_kind") not in {"agent", "mixed"}:
            raise HarnessError("test_definition", "故障修复必须提供目标相关的三层 Agent Case；局部 pytest 不能替代")
        preparation_source = source
        if not governance:
            definition = source.get("case_definition", {})
            preparation_source = {**source, "kind": "robot_task", "runtime_replay_required": True,
                "original_input": source.get("original_input") or definition.get("input", "")}
            if not preparation_source["original_input"]:
                raise HarnessError("material_missing", "原始任务输入缺失，无法建立三层回放")
        source = self.local_verifier.prepare({**task, "source": preparation_source}, candidate.path,
                                             output, proposal, check_cancel)
        if governance:
            agent_ids = ()
        else:
            source["kind"] = original.get("kind", "evaluation") if original else "robot_task"
            agent_source = self.evaluator.prepare_agent_case(source, check_cancel)
            source = {**source, "agent_source": agent_source} if original or source.get("test_sha256") else agent_source
            if original:
                source["original_case_source"] = original
            agent_ids = tuple(agent_source["case_ids"])
        local_ids = tuple(source.get("reproduction_ids", ()))
        original_ids = (original["case_id"],) if original else ()
        primary = tuple(dict.fromkeys((*local_ids, *original_ids, *agent_ids)))
        protected = tuple((original or source).get("passed_cases", ()))
        local_checks = source.get("case_ids", ()) if source.get("test_sha256") else ()
        checks = tuple(dict.fromkeys((*primary, *protected, *local_checks, *((original or {}).get("case_ids", ())))))
        real_agent = not governance
        repetitions = 3 if real_agent else 1
        repeat_map = {name: 3 if name in agent_ids else
                      int(original["repetitions"]) if original and name in original["case_ids"] else 1 for name in checks}
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
        if not governance:
            coverage["expected_behavior"] = sorted(set(coverage.get("expected_behavior", [])) | set(agent_ids) | set(original_ids))
        plan = VerificationPlan(primary, checks, protected,
                                repetitions, real_agent, source.get("agent_source", source).get("case_snapshot_id", ""), repeat_map, coverage)
        if source.get("kind", "evaluation") == "evaluation":
            source = {key: value for key, value in source.items() if key not in {"diagnosis", "preparation"}}
        return source, plan

    def run(self, task: VerificationRequest, candidate: CandidateRef, run_id: str,
            checks: list[str], check_cancel: Callable[[], None]) -> VerificationResult:
        source = task["source"]
        if source.get("agent_source"):
            agent_source = source["agent_source"]
            agent_ids = [name for name in checks if name in agent_source["case_ids"]]
            original = source.get("original_case_source")
            original_ids = [name for name in checks if original and name in original["case_ids"]]
            local_ids = [name for name in checks if name not in agent_ids and name not in original_ids]
            results = []
            if local_ids:
                receipt = self.local_verifier.run(task, candidate.path, run_id + "-local", local_ids, check_cancel)
                results.append(result_from_trials(receipt, "local-pytest", candidate))
            if original_ids:
                self._bind_external(task["task_id"], run_id + "-original")
                receipt = self.evaluator.run({**task, "source": original}, candidate.path,
                                             run_id + "-original", original_ids, check_cancel)
                results.append(result_from_trials(receipt, receipt["target_id"], candidate))
            if agent_ids:
                self._bind_external(task["task_id"], run_id + "-agent")
                receipt = self.evaluator.run({**task, "source": agent_source}, candidate.path, run_id + "-agent", agent_ids, check_cancel)
                observed = result_from_trials(receipt, receipt["target_id"], candidate)
                self._require_runtime_evidence(observed, agent_source)
                results.append(observed)
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
        result = replace(result_from_trials(receipt, source["target_id"] or receipt["target_id"], candidate), run_id=run_id)
        if source.get("runtime_replay_required"):
            self._require_runtime_evidence(result, source)
        return result

    @staticmethod
    def _require_runtime_evidence(result: VerificationResult, source: dict[str, Any]) -> None:
        for check in result.checks:
            if check.outcome != "passed":
                continue
            replay = check.evidence.get("execution", {}).get("metadata", {}).get("runtime_replay", {})
            denied = source["agent_case"].get("admission") == "denied"
            covered = replay.get("layers") == ["gateway"] and replay.get("admission") == "denied" if denied else (
                set(replay.get("layers", [])) == {"gateway", "application", "agent"})
            if not covered:
                raise HarnessError("verification_evidence", "通过结果缺少实际三层执行依据")

    def _bind_external(self, task_id: str, evaluation_id: str) -> None:
        current = self.store.get(task_id)
        delivery = current.get("delivery_evaluation")
        if delivery:
            self.store.update(task_id, delivery_evaluation={**delivery, "id": evaluation_id})
        else:
            self.store.update(task_id, current_evaluation_id=evaluation_id)

    def regressions(self, task: VerificationRequest, candidate: CandidateRef,
                    check_cancel: Callable[[], None], checks: list[str] | None = None) -> dict[str, Any]:
        value = self.local_verifier.regressions(task, candidate.path, check_cancel, checks)
        for name, row in value.get("rows", {}).items():
            if row["outcome"] == "error":
                raise HarnessError("verification_environment", "仓库回归发生执行环境错误")
            if name.startswith("repository:") and row["outcome"] != "passed":
                error = HarnessError("verification_evidence" if checks is None else "product_failure",
                                     "仓库静态检查未通过：" + name)
                error.evidence = {"checks": [{"name": name, "exit_code": row.get("exit_code", 1),
                    "failed_ids": [name], "diagnostic": row.get("message", "")} ]}
                raise error
        return value
