"""One strict v2 result codec at IPC, persistence and service read boundaries."""

from __future__ import annotations

from dataclasses import fields
import math
from typing import Any, Mapping

from chatcopilot.evals.artifact_ids import trial_artifact_id
from chatcopilot.evals.models import (
    TrialExecutionRequest, Assessment, CaseExpectation, EvaluationError, EvaluationTrial, ExecutionEvidence,
    JudgeResult, RESULT_SCHEMA_VERSION, to_jsonable,
)


class ResultContractError(ValueError):
    """A shared result boundary failed; continuing the batch is unsafe."""


class PipelineFailure(RuntimeError):
    def __init__(self, failure: EvaluationError):
        self.failure = failure
        super().__init__(failure.message)


class ArchivedResultError(ValueError):
    def __init__(self) -> None:
        super().__init__("旧格式测评已归档；仅支持摘要和原始导出，请创建新的测评。")


def require_current_result(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ArchivedResultError()


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResultContractError(f"{name}: expected object")
    return value


def _construct(cls: Any, value: Any, name: str) -> Any:
    raw = dict(_object(value, name))
    for field in fields(cls):
        if str(field.type).startswith("tuple[") and field.name in raw:
            if not isinstance(raw[field.name], (list, tuple)):
                raise ResultContractError(f"{name}.{field.name}: expected array")
            raw[field.name] = tuple(raw[field.name])
    if set(raw) - {f.name for f in fields(cls)}:
        raise ResultContractError(f"{name}: unknown fields")
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ResultContractError(f"{name}: missing or invalid fields") from exc


def trial_from_dict(payload: Mapping[str, Any]) -> EvaluationTrial:
    value = dict(payload)
    value.pop("case_instance_id", None)
    value["expectation"] = _construct(CaseExpectation, value.get("expectation"), "expectation")
    execution = _object(value.get("execution"), "execution")
    value["execution"] = _construct(ExecutionEvidence, {**execution, "events": tuple(execution.get("events", ()))}, "execution")
    assessment = value.get("assessment")
    if assessment is not None:
        assessment = dict(_object(assessment, "assessment"))
        if assessment.get("judge") is not None:
            assessment["judge"] = _construct(JudgeResult, assessment["judge"], "assessment.judge")
        value["assessment"] = _construct(Assessment, assessment, "assessment")
    if value.get("error") is not None:
        value["error"] = _construct(EvaluationError, value["error"], "error")
    trial = _construct(EvaluationTrial, value, "trial")
    validate_trial(trial)
    return trial


def validate_trial(trial: EvaluationTrial) -> None:
    for name in ("trial_id", "evaluation_id", "kind", "bot", "profile", "suite_id", "case_ref", "case_id",
                 "dimension", "target_id", "target_fingerprint", "executor", "backend", "model", "reasoning_effort"):
        if not isinstance(getattr(trial, name), str):
            raise ResultContractError(f"{name}: expected string")
    if trial.outcome not in {"passed", "failed", "error", "skipped"}:
        raise ResultContractError("outcome: invalid value")
    if not isinstance(trial.expectation, CaseExpectation) or not isinstance(trial.execution, ExecutionEvidence):
        raise ResultContractError("expectation/execution: invalid type")
    for name in ("behavior", "source"):
        if not isinstance(getattr(trial.expectation, name), str):
            raise ResultContractError(f"expectation.{name}: expected string")
    if not isinstance(trial.expectation.checks, (list, tuple)) or not all(isinstance(c, str) for c in trial.expectation.checks):
        raise ResultContractError("expectation.checks: expected string array")
    for name in ("metadata", "usage"):
        if not isinstance(getattr(trial.execution, name), dict):
            raise ResultContractError(f"execution.{name}: expected object")
    for name in ("final_text", "stop_reason", "started_at", "finished_at"):
        if not isinstance(getattr(trial.execution, name), str):
            raise ResultContractError(f"execution.{name}: expected string")
    for name in ("attempt", "order"):
        if type(getattr(trial, name)) is not int or getattr(trial, name) < 1:
            raise ResultContractError(f"{name}: expected positive integer")
    if trial.error is not None:
        if not isinstance(trial.error, EvaluationError):
            raise ResultContractError("error: expected structured error or null")
        for name in ("stage", "code", "message"):
            if not isinstance(getattr(trial.error, name), str) or not getattr(trial.error, name):
                raise ResultContractError(f"error.{name}: expected nonempty string")
    if trial.outcome == "error" and trial.error is None:
        raise ResultContractError("error: required for error outcome")
    if trial.outcome != "error" and trial.error is not None:
        raise ResultContractError("outcome: an error cannot be a scored outcome")
    if trial.assessment is not None and not isinstance(trial.assessment, Assessment):
        raise ResultContractError("assessment: invalid type")
    judge = trial.assessment.judge if trial.assessment else None
    if trial.outcome in {"passed", "failed"} and judge is None:
        raise ResultContractError("assessment.judge: required for scored outcome")
    if judge is not None:
        if type(judge.passed) is not bool:
            raise ResultContractError("assessment.judge.passed: expected boolean")
        for name in ("score", "max_score"):
            _number(getattr(judge, name), "assessment.judge." + name, nullable=False)
        if judge.max_score <= 0 or judge.score > judge.max_score:
            raise ResultContractError("assessment.judge.score: outside score range")
        if trial.outcome in {"passed", "failed"} and (trial.outcome == "passed") != judge.passed:
            raise ResultContractError("outcome: inconsistent assessment")
    _number(trial.execution.total_seconds, "execution.total_seconds")
    if trial.assessment:
        _number(trial.assessment.duration_seconds, "assessment.duration_seconds")


def _number(value: Any, name: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ResultContractError(f"{name}: expected finite nonnegative number")


def trial_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read model derived only from v2, not a legacy-format conversion."""
    trial = trial_from_dict(payload)
    return {**dict(payload), "score": trial.score, "max_score": trial.max_score,
            "passed": trial.passed, "final_text": trial.final_text, "stop_reason": trial.stop_reason,
            "duration_seconds": trial.duration_seconds, "started_at": trial.started_at,
            "finished_at": trial.finished_at, "events": list(trial.events), "judge": trial.judge,
            "usage_totals": trial.usage_totals, "tool_summary": trial.tool_summary, "evidence": trial.evidence}


def validate_result(payload: Mapping[str, Any]) -> None:
    require_current_result(payload)
    if not isinstance(payload.get("trials"), list):
        raise ResultContractError("result.trials: expected array")
    for trial in payload["trials"]:
        decoded = trial_from_dict(trial)
        if decoded.evaluation_id != payload.get("evaluation_id"):
            raise ResultContractError("trial.evaluation_id: result identity mismatch")


def error_from_exception(exc: BaseException, stage: str) -> EvaluationError:
    if isinstance(exc, PipelineFailure):
        return exc.failure
    declared = getattr(exc, "code", "")
    if declared in {"execution_error", "evidence_missing"} or isinstance(declared, str) and declared.startswith("capability_"):
        return EvaluationError("scoring" if declared == "evidence_missing" else "execution", declared,
                               f"{type(exc).__name__}: {exc}"[:4096])
    if isinstance(exc, ResultContractError):
        stage, code = "result_validation", "result_contract_error"
    else:
        code = {"scoring": "judge_error", "judging": "judge_error", "persistence": "storage_error",
                "cleanup": "cleanup_error", "protocol": "protocol_error"}.get(stage, "execution_error")
    return EvaluationError(stage, code, f"{type(exc).__name__}: {exc}"[:4096])


def assessment_from_dict(value: Any) -> Assessment | None:
    if value is None:
        return None
    raw = dict(_object(value, "assessment"))
    if raw.get("judge") is not None:
        raw["judge"] = _construct(JudgeResult, raw["judge"], "assessment.judge")
    return _construct(Assessment, raw, "assessment")


_MAX_TRIAL_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_TRIAL_EVENTS = 512
_MAX_TRIAL_COLLECTION_ITEMS = 4096
_MAX_TRIAL_JSON_NODES = 20_000
_MAX_TRIAL_JSON_DEPTH = 12
_MAX_TRIAL_STRING_CHARS = 128 * 1024
_MAX_TRIAL_KEY_CHARS = 256
def _assert_bounded_trial(trial: EvaluationTrial) -> None:
    validate_trial(trial)
    if len(trial.events) > _MAX_TRIAL_EVENTS:
        raise ResultContractError(f"execution.events: more than {_MAX_TRIAL_EVENTS} events")
    try:
        _assert_bounded_json_value(trial.execution.metadata, depth=0, budget=[_MAX_TRIAL_JSON_NODES], active=set())
        _assert_bounded_json_value(trial.expectation.reference_answer, depth=0, budget=[_MAX_TRIAL_JSON_NODES], active=set())
        # The v2 envelope adds two structural levels before the original evidence.
        _assert_bounded_json_value(to_jsonable(trial), depth=-2,
                                  budget=[_MAX_TRIAL_JSON_NODES], active=set())
    except ValueError as exc:
        raise ResultContractError(str(exc)) from exc


def _assert_bounded_json_value(
    value: Any,
    *,
    depth: int,
    budget: list[int],
    active: set[int],
) -> None:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError(f"Trial contains more than {_MAX_TRIAL_JSON_NODES} JSON nodes")
    if depth > _MAX_TRIAL_JSON_DEPTH:
        raise ValueError(f"Trial JSON nesting exceeds {_MAX_TRIAL_JSON_DEPTH}")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Trial contains NaN or Infinity")
        return
    if isinstance(value, str):
        if len(value) > _MAX_TRIAL_STRING_CHARS:
            raise ValueError("Trial contains an oversized string")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_TRIAL_COLLECTION_ITEMS:
            raise ValueError("Trial contains an oversized mapping")
        identity = id(value)
        if identity in active:
            raise ValueError("Trial contains a recursive mapping")
        active.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > _MAX_TRIAL_KEY_CHARS:
                    raise ValueError("Trial contains an invalid mapping key")
                _assert_bounded_json_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                )
        finally:
            active.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_TRIAL_COLLECTION_ITEMS:
            raise ValueError("Trial contains an oversized collection")
        identity = id(value)
        if identity in active:
            raise ValueError("Trial contains a recursive collection")
        active.add(identity)
        try:
            for item in value:
                _assert_bounded_json_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                )
        finally:
            active.remove(identity)
        return
    raise ValueError(f"Trial contains a non-JSON value: {type(value).__name__}")



def _trial_id(request: TrialExecutionRequest) -> str:
    return trial_artifact_id(
        request.case.case_id,
        attempt=request.attempt,
        target_fingerprint=request.target.fingerprint,
    )



def validate_checkpoint(value: dict[str, Any]) -> None:
    _assert_bounded_json_value(value, depth=-2, budget=[_MAX_TRIAL_JSON_NODES], active=set())
    if "expectation" in value:
        _construct(CaseExpectation, value["expectation"], "expectation")
    if "observation" in value:
        observation = _object(value["observation"], "observation")
        for name in ("final_text", "stop_reason"):
            if not isinstance(observation.get(name), str):
                raise ResultContractError(f"observation.{name}: expected string")
        for name in ("events", "tool_calls", "produced_resources", "evidence"):
            if not isinstance(observation.get(name), list):
                raise ResultContractError(f"observation.{name}: expected array")
    if "assessment" in value:
        assessment_from_dict(value["assessment"])
