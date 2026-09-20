"""Harness adapter using only the public Evaluation service client."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.evals.service import (
    EvaluationServiceClient,
    RESULT_SCHEMA_VERSION,
    EvaluationServiceError,
    EvaluationServiceUnavailable,
)
from chatcopilot.harness.models import Cancelled, HarnessError, passed_cases
from chatcopilot.harness.control_types import EVALUATION_TERMINAL_STATES


class ServiceEvaluator:
    def __init__(
        self, client: EvaluationServiceClient | None = None, *, socket_path: Path | None = None
    ) -> None:
        self.client = client or EvaluationServiceClient(socket_path)

    @staticmethod
    def _read_once_more(operation, check_cancel=lambda: None):
        """Retry a read at most once; a mutation with an uncertain acknowledgement is never replayed."""
        for attempt in range(2):
            check_cancel()
            try:
                return operation()
            except EvaluationServiceUnavailable:
                if attempt:
                    raise
                time.sleep(0.1)

    def load(self, evaluation_id: str, *, target_id: str | None = None) -> dict[str, Any]:
        record = self.client.get(evaluation_id)
        result = record.get("result") or {}
        failures = {}
        if record.get("archived"):
            return {"kind": "evaluation", "evaluation_id": evaluation_id, "status": record["status"],
                    "failures": [], "blockers": ["旧格式测评已归档，请创建新的测评"]}
        for trial in result.get("trials", []):
            if _repairable(trial):
                key = (trial["case_ref"], trial["target_id"])
                failures[key] = {name: trial[name] for name in ("case_id", "case_ref", "target_id")}
        blockers = []
        if record.get("status") != "completed" or not record.get("conditions"):
            blockers.append("需要已完成且具有完整 Case 快照的 Suite 测评")
        request = record.get("request") or {}
        if (
            request.get("kind") != "suite"
            or request.get("dry_run")
            or request.get("confirm_external_write")
        ):
            blockers.append("只支持实际执行的隔离 Suite 测评")
        if not failures:
            blockers.append("该测评没有失败 Case")
        if target_id is not None:
            target = next((item for item in result.get("targets", []) if item["target_id"] == target_id), None)
            if target is None or target["executor"] in {"direct_llm", "dry_run"}:
                blockers.append("此目标未执行 AgentStrata 运行时")
        return {
            "kind": "evaluation",
            "evaluation_id": evaluation_id,
            "bot_id": request.get("bot_id", record.get("bot_id", "")),
            "status": record["status"],
            "repetitions": request.get("repetitions", 1),
            "failures": list(failures.values()),
            "blockers": blockers,
        }

    def _instance(self, case_instance_id: str) -> dict[str, Any]:
        value = self.client.case_instance(case_instance_id)
        if value.get("case_instance_id") != case_instance_id:
            raise HarnessError("source_mismatch", "Case 实例 ID 与来源不一致")
        return value

    def load_instance(self, case_instance_id: str) -> dict[str, Any]:
        instance = self._instance(case_instance_id)
        value = self.load(instance["evaluation_id"], target_id=instance["target_id"])
        value.pop("failures", None)
        trial = instance["trial"]
        reference = trial.get("execution", {}).get("metadata", {}).get("trace") or {}
        if not reference.get("trace_ref") or reference.get("capture_state") != "available":
            value["blockers"].append("此 Case 没有完整本地执行归档，请采集新的测评")
        if not _repairable(trial):
            value["blockers"].append("该 Case 实例没有失败或执行错误，不能发起修复")
        value["case_instance"] = {
            **{key: item for key, item in instance.items() if key != "trial"},
            "case_id": trial["case_id"], "outcome": trial["outcome"],
        }
        return value

    def source_instance(self, case_instance_id: str) -> dict[str, Any]:
        instance = self._instance(case_instance_id)
        if not _repairable(instance["trial"]):
            raise HarnessError("not_failed", "该 Case 实例没有失败或执行错误")
        source = self.source(instance["evaluation_id"], instance["case_ref"], instance["target_id"], include_trace=False)
        if not any(trial.get("trial_id") == instance["trial_id"]
                   and trial.get("attempt") == instance["attempt"]
                   and trial.get("outcome") in {"failed", "error"} for trial in source["trials"]):
            raise HarnessError("source_mismatch", "Case 实例结果已变化，请重新加载")
        reference = instance["trial"].get("execution", {}).get("metadata", {}).get("trace") or {}
        if reference.get("trace_ref") and reference.get("capture_state") == "available":
            bundle = self.client.trace_export(case_instance_id)
            source = {**source, "trace_bundle": bundle}
        else:
            source = {**source, "blockers": [*source.get("blockers", []),
                       "旧 Case 没有本地执行归档，请采集新的测评"]}
        return {**source, "case_instance_id": case_instance_id,
                "trial_id": instance["trial_id"], "attempt": instance["attempt"]}

    def source(self, evaluation_id: str, case_ref: str, target_id: str, *, include_trace: bool = True) -> dict[str, Any]:
        record = self.client.get(evaluation_id)
        if record.get("archived") or record.get("result", {}).get("schema_version") != RESULT_SCHEMA_VERSION:
            raise HarnessError("source_archived", "旧格式测评已归档，请创建新的测评")
        if record.get("status") != "completed" or not record.get("conditions"):
            raise HarnessError("unsupported_source", "需要已完成、具有完整 Case 快照的 Suite 测评")
        request, result = record["request"], record["result"]
        if (
            request.get("kind") != "suite"
            or request.get("dry_run")
            or request.get("confirm_external_write")
        ):
            raise HarnessError("unsupported_source", "Harness 仅接受真实、隔离的 Suite 测评")
        target = next((item for item in result["targets"] if item["target_id"] == target_id), None)
        if target is None or target["executor"] in {"direct_llm", "dry_run"}:
            raise HarnessError("unsupported_target", "此目标未执行 AgentStrata 运行时")
        detail = self.client.case_detail(evaluation_id, case_ref, target_id=target_id)
        trials = detail["trials"]
        if not trials or not any(_repairable(trial) for trial in trials):
            raise HarnessError("not_failed", "选中的 Case 没有失败 Trial")
        case_id = trials[0]["case_id"]
        definition = result["config_snapshot"]["definition_snapshot"]
        cases = [item["case_id"] for item in definition["cases"]]
        repeats = request["repetitions"]
        passed = passed_cases(result, target_id, cases, repeats)
        request_keys = (
            "kind",
            "suite_id",
            "repetitions",
            "seed",
            "max_wall_seconds",
            "options",
            "llm_judge",
            "case_snapshot_id",
        )
        signature = []
        for trial in trials:
            if trial["outcome"] not in {"failed", "error"}:
                continue
            error = trial.get("error") or {}
            signature.append(
                {
                    "outcome": trial["outcome"],
                    "stop_reason": trial.get("execution", {}).get("stop_reason"),
                    "error_code": error.get("code"),
                    "error_stage": error.get("stage"),
                    "judge": ((trial.get("assessment") or {}).get("judge") or {}).get("reasons", []),
                }
            )
        selected_definition = next(item for item in definition["cases"] if item["case_id"] == case_id)
        source = {
            **({"agent_case": selected_definition["agent_case"], "case_snapshot_id": case_id}
               if selected_definition.get("agent_case") else {}),
            "kind": "evaluation",
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "evaluation_id": evaluation_id,
            "bot_id": request["bot_id"],
            "suite_id": request["suite_id"],
            "case_ref": case_ref,
            "case_id": case_id,
            "target_id": target_id,
            "executor": target["executor"],
            "case_ids": cases,
            "repetitions": repeats,
            "passed_cases": sorted(passed),
            "conditions": record["conditions"],
            "request": {key: request[key] for key in request_keys if key in request},
            "trials": trials,
            "failure_signature": signature,
            "case_definition": next(
                item for item in definition["cases"] if item["case_id"] == case_id
            ),
        }
        if include_trace:
            selected = next(trial for trial in trials if _repairable(trial))
            reference = selected.get("execution", {}).get("metadata", {}).get("trace") or {}
            if reference.get("capture_state") == "available" and selected.get("case_instance_id"):
                source["trace_bundle"] = self.client.trace_export(selected["case_instance_id"])
            else:
                source["blockers"] = ["此 Case 没有完整本地执行归档，请采集新的测评"]
        return source

    def validate_agent_case(self, case: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._read_once_more(lambda: self.client.validate_case(case))
        except EvaluationServiceUnavailable as exc:
            raise HarnessError("evaluation_unavailable", "验证能力服务暂不可用") from exc
        except EvaluationServiceError as exc:
            # Invalid declarations are author-correctable; transport faults are not.
            if exc.code == "fixture_missing":
                raise HarnessError("fixture_missing", exc.message) from exc
            if exc.code == "invalid_request":
                raise ValueError(exc.message) from exc
            raise HarnessError("verification_environment", exc.message) from exc

    def capabilities(self) -> dict[str, Any]:
        try:
            return self._read_once_more(self.client.case_capabilities)
        except EvaluationServiceUnavailable as exc:
            raise HarnessError("evaluation_unavailable", "验证能力服务暂不可用；未启动模型") from exc

    def prepare_agent_case(self, source: dict[str, Any], check_cancel: Callable[[], None]) -> dict[str, Any]:
        check_cancel()
        case = {**source["agent_case"], "runtime_replay": True}
        if case.get("admission", "allowed") != source.get("evidence", {}).get("admission", "allowed"):
            raise HarnessError("source_mismatch", "复现不能改变原任务的准入条件")
        # The draft may use synthetic inputs, but cannot change the observed actor's role.
        role = (source.get("evidence", {}).get("run") or {}).get("role")
        if role and case["role"] != role:
            raise HarnessError("source_mismatch", "复现不能改变原任务主体的权限角色")
        conversation = (source.get("evidence", {}).get("run") or {}).get("conversation_kind")
        channel_kind = {"group": "group", "p2p": "private", "private": "private"}.get(conversation)
        if channel_kind and case["channel_kind"] != channel_kind:
            raise HarnessError("source_mismatch", "复现不能改变原任务的会话类型")
        snapshot = self.client.register_case(case)
        case = snapshot["case"]
        return {**{key: value for key, value in source.items() if key not in {"conditions", "agent_source", "original_case_source"}},
                "agent_case": case, "case_snapshot_id": snapshot["snapshot_id"],
                "case_id": snapshot["snapshot_id"], "case_ids": [snapshot["snapshot_id"]],
                "target_id": "", "passed_cases": [], "repetitions": 3,
                "suite_id": "agentstrata-regression-v1", "case_ref": "agentstrata-regression-v1:" + snapshot["snapshot_id"],
                "request": {"kind": "suite", "suite_id": "agentstrata-regression-v1",
                            "case_snapshot_id": snapshot["snapshot_id"], "repetitions": 3,
                            "seed": 0, "max_wall_seconds": 0, "options": {}}}

    def run(
        self,
        task: dict[str, Any],
        worktree: Path,
        evaluation_id: str,
        case_ids: list[str],
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        source = task["source"]
        request = {**source["request"], "repetitions": source["repetitions"], "case_ids": case_ids, "preset": "custom"}
        descriptor = {"path": str(worktree), "sha256": manifest_digest(source_manifest(worktree))}
        try:
            check_cancel()
            try:
                record = self._read_once_more(lambda: self.client.get(evaluation_id), check_cancel)
            except EvaluationServiceError as exc:
                if exc.code != "not_found":
                    raise
                check_cancel()
                record = self.client.start(
                    bot_id=source["bot_id"],
                    request=request,
                    evaluation_id=evaluation_id,
                    code_source=descriptor,
                    expected_conditions=source.get("conditions"),
                )
            while record["status"] in {"queued", "running"}:
                check_cancel()
                time.sleep(0.5)
                record = self._read_once_more(lambda: self.client.get(evaluation_id), check_cancel)
            check_cancel()
        except Cancelled:
            self.cancel_confirmed(evaluation_id)
            raise
        except HarnessError as exc:
            if exc.code == "budget_exhausted":
                self.cancel_confirmed(evaluation_id)
            raise
        except EvaluationServiceUnavailable as exc:
            raise HarnessError(
                "evaluation_unavailable", "测评服务暂不可用；继续时将按原 ID 查询"
            ) from exc
        if record["status"] != "completed":
            raise HarnessError(
                "evaluation_incomplete",
                "测评未完成：" + str(record.get("error") or record["status"]),
            )
        if record.get("result_storage") != "database":
            raise HarnessError("result_pending", "验证结果尚未可靠入库，不能确认修复成功")
        if record.get("code_source", {}).get("sha256") != descriptor["sha256"]:
            raise HarnessError("source_changed", "测评结果与当前候选代码不一致")
        result = record.get("result") or {}
        # The complete identity matrix, rather than an aggregate score, is the gate.
        target = source["target_id"] or result["targets"][0]["target_id"]
        observed = (source.get("evidence", {}).get("run") or {})
        actual_target = next((item for item in result["targets"] if item["target_id"] == target), {})
        if any(observed.get(key) and actual_target.get(key) != observed[key] for key in ("model", "backend")):
            raise HarnessError("source_mismatch", "验证模型或 backend 与原任务不同，不能作为原目标的验收证据")
        passed_cases(result, target, case_ids, source["repetitions"])
        return {
            "evaluation_id": evaluation_id,
            "result": result,
            "target_id": target,
            "conditions": record.get("conditions"),
            "code_source": record["code_source"],
        }

    def cancel(self, evaluation_id: str) -> None:
        try:
            self.client.cancel(evaluation_id)
        except EvaluationServiceError as exc:
            if exc.code != "not_found":
                raise

    def execution_status(self, evaluation_id: str) -> str:
        try:
            return str(self.client.get(evaluation_id)["status"])
        except EvaluationServiceError as exc:
            if exc.code != "not_found":
                raise
            return "not_found"

    def cancel_confirmed(self, evaluation_id: str) -> None:
        """Keep the workflow's external reference until stop is confirmed."""
        try:
            if self.execution_status(evaluation_id) in EVALUATION_TERMINAL_STATES:
                return
            try:
                self.cancel(evaluation_id)
            except EvaluationServiceError:
                if self.execution_status(evaluation_id) in EVALUATION_TERMINAL_STATES:
                    return
                raise
            if self.execution_status(evaluation_id) not in EVALUATION_TERMINAL_STATES:
                raise HarnessError("result_pending", "测评取消尚未确认结束，保留原 ID 等待核对")
        except EvaluationServiceError as exc:
            raise HarnessError("evaluation_unavailable", "无法确认测评已取消，保留原 ID 等待核对") from exc


def _repairable(trial: dict[str, Any]) -> bool:
    error = trial.get("error")
    return trial.get("outcome") == "failed" or (
        trial.get("outcome") == "error" and isinstance(error, dict)
        and error.get("code") in {"execution_error", "case_timeout"}
    )
