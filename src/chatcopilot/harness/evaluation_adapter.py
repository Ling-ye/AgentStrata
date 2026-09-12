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


class ServiceEvaluator:
    def __init__(
        self, client: EvaluationServiceClient | None = None, *, socket_path: Path | None = None
    ) -> None:
        self.client = client or EvaluationServiceClient(socket_path)

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
        source = self.source(instance["evaluation_id"], instance["case_ref"], instance["target_id"])
        if not any(trial.get("trial_id") == instance["trial_id"]
                   and trial.get("attempt") == instance["attempt"]
                   and trial.get("outcome") in {"failed", "error"} for trial in source["trials"]):
            raise HarnessError("source_mismatch", "Case 实例结果已变化，请重新加载")
        return {**source, "case_instance_id": case_instance_id,
                "trial_id": instance["trial_id"], "attempt": instance["attempt"]}

    def source(self, evaluation_id: str, case_ref: str, target_id: str) -> dict[str, Any]:
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
        return {
            "kind": "evaluation",
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "evaluation_id": evaluation_id,
            "bot_id": request["bot_id"],
            "suite_id": request["suite_id"],
            "case_ref": case_ref,
            "case_id": case_id,
            "target_id": target_id,
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

    def run(
        self,
        task: dict[str, Any],
        worktree: Path,
        evaluation_id: str,
        case_ids: list[str],
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]:
        source = task["source"]
        check_cancel()
        request = {**source["request"], "case_ids": case_ids, "preset": "custom"}
        descriptor = {"path": str(worktree), "sha256": manifest_digest(source_manifest(worktree))}
        try:
            try:
                record = self.client.get(evaluation_id)
            except EvaluationServiceError as exc:
                if exc.code != "not_found":
                    raise
                check_cancel()
                record = self.client.start(
                    bot_id=source["bot_id"],
                    request=request,
                    evaluation_id=evaluation_id,
                    code_source=descriptor,
                    expected_conditions=source["conditions"],
                )
            while record["status"] in {"queued", "running"}:
                check_cancel()
                time.sleep(0.5)
                record = self.client.get(evaluation_id)
            check_cancel()
        except Cancelled:
            self.client.cancel(evaluation_id)
            raise
        except HarnessError as exc:
            if exc.code == "budget_exhausted":
                self.client.cancel(evaluation_id)
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
        passed_cases(result, source["target_id"], case_ids, source["repetitions"])
        return {
            "evaluation_id": evaluation_id,
            "result": result,
            "code_source": record["code_source"],
        }

    def cancel(self, evaluation_id: str) -> None:
        try:
            self.client.cancel(evaluation_id)
        except EvaluationServiceError as exc:
            if exc.code != "not_found":
                raise


def _repairable(trial: dict[str, Any]) -> bool:
    error = trial.get("error")
    return trial.get("outcome") == "failed" or (
        trial.get("outcome") == "error" and isinstance(error, dict)
        and error.get("code") in {"execution_error", "case_timeout"}
    )
