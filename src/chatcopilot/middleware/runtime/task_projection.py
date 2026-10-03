"""Task record format and pure bounded projections; no file or environment reads."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Mapping

from chatcopilot.contracts.identity import stable_actor_ref
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED, WorkspaceView

TASK_SCHEMA_VERSION = 2
TASKS_DIRNAME = "tasks"
TASK_FILENAME = "task.json"
EVENTS_FILENAME = "events.jsonl"
EVENT_SEQUENCE_FILENAME = ".events.sequence"
COMPLETION_LOCK_FILENAME = ".completion.lock"
TURN_FILENAME = "turn.json"
CONTEXTS_DIRNAME = "contexts"
GROUP_TASK_ACTORS_DIRNAME = "task-actors"
GROUP_TASK_INTAKE_DIRNAME = "task-intake"
MAX_CONTEXT_ARTIFACT_BYTES = 8 * 1024 * 1024
ACTIVITY_SUMMARY_WRITE_INTERVAL_SECONDS = 0.25
MAX_PROVIDER_ACTIVITY_SUMMARIES = 500
MAX_PROVIDER_ACTIVITY_RAW_EVENTS = MAX_PROVIDER_ACTIVITY_SUMMARIES * 2
MAX_TASK_TOOL_SUMMARIES = 1000
MAX_TASK_STEP_SUMMARIES = 1000
MAX_TASK_LLM_CALL_SUMMARIES = 1000
MAX_TASK_CONTEXT_SNAPSHOT_SUMMARIES = 5000
MAX_TASK_INPUT_RESOURCE_SUMMARIES = 500
MAX_INPUT_RESOURCES_PER_SUMMARY = 20
MAX_TASK_EVENT_BYTES = 64 * 1024
MAX_EVENT_SEQUENCE = (1 << 63) - 1
MAX_USAGE_TOTAL = (1 << 63) - 1
MAX_TASK_SUMMARY_BYTES = 8 * 1024 * 1024
MAX_JOB_RESULT_SUMMARIES = 1000
MAX_JOB_RESULT_OUTPUTS = 8
MAX_JOB_RESULT_TEXT_CHARS = 1024
MAX_JOB_RESULT_OUTPUT_CHARS = 512
_MAX_EVENT_SEQUENCE_STATE_BYTES = len(str(MAX_EVENT_SEQUENCE))
_EVENT_LOCK_TIMEOUT_SECONDS = 0.25
_COMPLETION_LOCK_TIMEOUT_SECONDS = 5.0
_PROVIDER_ACTIVITY_KINDS = frozenset(
    {
        "command",
        "reasoning",
        "mcp_tool",
        "web_search",
        "file_change",
        "plan",
        "provider_event",
    }
)
_PROVIDER_OMISSION_KIND = "provider_omission"
_JOB_ID_RE = re.compile(r"\bjob_\d{8}_\d{6}_[0-9a-fA-F]{8}\b")
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_.-]{1,159}$")
_CONTEXT_ID_RE = re.compile(r"^ctx_[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_ARTIFACT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_TRUNCATED_ORIGINAL_CHARS_RE = re.compile(r"\[ORIGINAL_CHARS=(\d+)\]")


def describe_user_text(text: str, *, limit: int = 120) -> str:
    first_line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    if not first_line:
        return "（空消息）"
    return first_line if len(first_line) <= limit else first_line[: limit - 1] + "…"


def _workspace_payload(
    workspace: WorkspaceView,
    *,
    redact_identity: bool = False,
    unauthenticated_intake: bool = False,
) -> Dict[str, Any]:
    shared_group = workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
    actor_ref = (
        stable_actor_ref(
            "qq",
            workspace.user_id or "",
            conversation_id=f"{workspace.chat_kind or ''}:{workspace.chat_id or ''}",
        )
        if workspace.user_id
        and not (unauthenticated_intake and shared_group)
        and (shared_group or redact_identity)
        else None
    )
    return {
        "root": str(workspace.root),
        "chat_kind": workspace.chat_kind,
        "chat_id": None if shared_group or redact_identity else workspace.chat_id,
        "user_id": None if shared_group or redact_identity else workspace.user_id,
        "user_name": None if shared_group or redact_identity else workspace.user_name,
        "actor_ref": actor_ref,
    }


def _replace_identity_literals(value: Any, literals: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        safe = value
        for literal in sorted(set(literals), key=len, reverse=True):
            safe = safe.replace(literal, "[REDACTED_IDENTITY]")
        return safe
    if isinstance(value, list):
        return [_replace_identity_literals(item, literals) for item in value]
    if isinstance(value, dict):
        return {
            _replace_identity_literals(key, literals): _replace_identity_literals(
                item,
                literals,
            )
            for key, item in value.items()
        }
    return value


def _redact_workspace_identity(
    payload: Any,
    workspace: WorkspaceView,
    *,
    force: bool = False,
) -> Any:
    if not force and workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        return payload
    literals = tuple(
        value
        for value in (
            workspace.chat_id,
            workspace.user_id,
            workspace.user_name,
        )
        if value
    )
    return _replace_identity_literals(payload, literals)


def _redact_group_turn_content(
    payload: Any,
    workspace: WorkspaceView,
    *,
    user_text: str,
    message_id: str | None,
) -> Any:
    if workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        return payload

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            safe = value
            if message_id:
                safe = safe.replace(message_id, "[REDACTED_MESSAGE]")
            if user_text:
                safe = safe.replace(user_text, "[REDACTED_GROUP_TURN_TEXT]")
            return safe
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    return redact(payload)


def _extract_job_ids(*parts: object) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    for part in parts:
        text = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False, default=str)
        for job_id in _JOB_ID_RE.findall(text):
            if job_id not in seen:
                found.append(job_id)
                seen.add(job_id)
    return found


def _empty_usage_totals() -> Dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "llm_calls": 0,
        "cache_hit_calls": 0,
        "cache_hit_rate": 0.0,
        "cache_hit_call_rate": 0.0,
    }


def _saturating_nonnegative_add(left: Any, right: Any) -> int:
    def bounded(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return min(value, MAX_USAGE_TOTAL)

    return min(MAX_USAGE_TOTAL, bounded(left) + bounded(right))


def _private_json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _bounded_string(value: Any, limit: int) -> tuple[str, bool]:
    try:
        text = str(value or "")
    except (ValueError, RecursionError):
        return "[invalid text]", True
    return (text, False) if len(text) <= limit else (text[: limit - 1] + "…", True)


def _payload_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        encoded = b"[unserializable]"
    return hashlib.sha256(encoded).hexdigest()


def _bounded_job_result(item: Any, *, compact: bool = False) -> Dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    summary, summary_truncated = _bounded_string(
        source.get("summary"),
        MAX_JOB_RESULT_TEXT_CHARS,
    )
    error, error_truncated = _bounded_string(
        source.get("error"),
        MAX_JOB_RESULT_TEXT_CHARS,
    )
    output_values, output_total, invalid_outputs = _bounded_collection(
        source.get("outputs"),
        MAX_JOB_RESULT_OUTPUTS,
    )
    raw_outputs = [value for value in output_values if isinstance(value, str)]
    outputs: list[str] = []
    output_text_truncated = False
    for value in raw_outputs:
        bounded, was_truncated = _bounded_string(value, MAX_JOB_RESULT_OUTPUT_CHARS)
        outputs.append(bounded)
        output_text_truncated = output_text_truncated or was_truncated
    omissions = [
        field
        for field in source.get("omitted_fields") or []
        if field in {"summary", "error", "outputs"}
    ]
    if summary_truncated:
        omissions.append("summary")
    if error_truncated:
        omissions.append("error")
    if invalid_outputs or output_total > len(outputs) or output_text_truncated:
        omissions.append("outputs")
    if compact:
        omissions.extend(
            field
            for field, value in (("summary", summary), ("error", error), ("outputs", outputs))
            if value and field not in omissions
        )
        summary = ""
        error = ""
        outputs = []
    result = {
        "job_id": _bounded_text(source.get("job_id"), 256),
        "ok": bool(source.get("ok")),
        "status": _bounded_text(source.get("status"), 64),
        "stage": _bounded_text(source.get("stage"), 256),
        "error_code": _bounded_text(source.get("error_code"), 256),
        "summary": summary,
        "error": error,
        "outputs": outputs,
        "output_count": output_total,
        "finished_at": _bounded_observed_number(source.get("finished_at")),
    }
    if omissions or bool(source.get("payload_truncated")):
        result.update(
            {
                "payload_truncated": True,
                "omitted_fields": sorted(set(omissions)),
                "payload_sha256": str(source.get("payload_sha256") or _payload_digest(source)),
            }
        )
    return result


def _bounded_job_results(values: Any, *, compact: bool = False) -> list[Dict[str, Any]]:
    items, _, _ = _bounded_collection(values, MAX_JOB_RESULT_SUMMARIES)
    return [_bounded_job_result(item, compact=compact) for item in items]


def _bounded_collection(
    value: Any,
    limit: int,
    *,
    keep_latest: bool = False,
) -> tuple[list[Any], int, bool]:
    if not isinstance(value, (list, tuple)):
        return [], 0, value not in (None, [])
    total = len(value)
    retained = value[-limit:] if keep_latest and limit > 0 else value[:limit]
    return list(retained), total, total > limit


def _bounded_nonnegative_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_USAGE_TOTAL)


def _bounded_observed_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(-MAX_USAGE_TOTAL, min(value, MAX_USAGE_TOTAL))
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _task_llm_call_summaries(values: Any) -> tuple[list[Dict[str, Any]], int, bool]:
    items, total, truncated = _bounded_collection(
        values,
        MAX_TASK_LLM_CALL_SUMMARIES,
        keep_latest=True,
    )
    summaries: list[Dict[str, Any]] = []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        raw_usage = item.get("usage")
        usage: Dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        summaries.append(
            {
                "model": _bounded_text(item.get("model"), 512),
                "runtime_id": _bounded_text(item.get("runtime_id"), 128),
                "iteration": _bounded_nonnegative_integer(item.get("iteration")),
                "finish_reason": _bounded_text(item.get("finish_reason"), 512),
                "usage": _normalize_usage_payload(usage),
                "trace_id": _bounded_text(item.get("trace_id"), 256),
                "span_id": _bounded_text(item.get("span_id"), 256),
                "parent_span_id": _bounded_text(item.get("parent_span_id"), 256),
                "depth": _bounded_nonnegative_integer(item.get("depth")),
                "role": _bounded_text(item.get("role"), 32),
                "started_at": _bounded_observed_number(item.get("started_at")),
                "recorded_at": _bounded_observed_number(item.get("recorded_at")),
                "input_message_count": _bounded_nonnegative_integer(
                    item.get("input_message_count")
                ),
                "input_estimated_tokens": _bounded_nonnegative_integer(
                    item.get("input_estimated_tokens")
                ),
                "system_estimated_tokens": _bounded_nonnegative_integer(
                    item.get("system_estimated_tokens")
                ),
                "tool_schema_count": _bounded_nonnegative_integer(item.get("tool_schema_count")),
                "tool_schema_estimated_tokens": _bounded_nonnegative_integer(
                    item.get("tool_schema_estimated_tokens")
                ),
                "estimator_version": _bounded_text(item.get("estimator_version"), 128),
                "context_kind": _bounded_text(item.get("context_kind"), 128),
                "context_snapshot_id": _bounded_text(
                    item.get("context_snapshot_id"),
                    128,
                ),
                "step_id": _bounded_text(item.get("step_id"), 256),
                "ok": bool(item.get("ok", True)),
            }
        )
    return summaries, total, truncated


def _task_context_snapshot_summaries(
    values: Any,
    *,
    minimal: bool = False,
) -> tuple[list[Dict[str, Any]], int, bool]:
    items, total, truncated = _bounded_collection(
        values,
        MAX_TASK_CONTEXT_SNAPSHOT_SUMMARIES,
        keep_latest=True,
    )
    summaries: list[Dict[str, Any]] = []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        summary: Dict[str, Any] = {
            # This identity is the Console authorization/index key for the
            # separate context artifact and must survive total-size fallback.
            "snapshot_id": _bounded_text(item.get("snapshot_id"), 128),
            "capture_status": _bounded_text(item.get("capture_status"), 64),
            "runtime_id": _bounded_text(item.get("runtime_id"), 128),
            "model": _bounded_text(item.get("model"), 128 if minimal else 512),
            "coverage": _bounded_text(item.get("coverage"), 64),
            "truncated": bool(item.get("truncated")),
            "captured_at": _bounded_observed_number(item.get("captured_at")),
        }
        if not minimal:
            omitted_items, _, omitted_truncated = _bounded_collection(
                item.get("omitted"),
                20,
            )
            summary.update(
                {
                    "redacted": bool(item.get("redacted")),
                    "iteration": _bounded_nonnegative_integer(item.get("iteration")),
                    "message_count": _bounded_nonnegative_integer(item.get("message_count")),
                    "effective_message_count": _bounded_nonnegative_integer(
                        item.get("effective_message_count")
                    ),
                    "tool_schema_count": _bounded_nonnegative_integer(
                        item.get("tool_schema_count")
                    ),
                    "resource_count": _bounded_nonnegative_integer(item.get("resource_count")),
                    "estimated_tokens": _bounded_nonnegative_integer(item.get("estimated_tokens")),
                    "reasoning_effort": _bounded_text(
                        item.get("reasoning_effort"),
                        64,
                    ),
                    "context_kind": _bounded_text(item.get("context_kind"), 128),
                    "omitted": [_bounded_text(value, 128) for value in omitted_items],
                    "omitted_truncated": omitted_truncated,
                    "trace_id": _bounded_text(item.get("trace_id"), 256),
                    "span_id": _bounded_text(item.get("span_id"), 256),
                    "parent_span_id": _bounded_text(item.get("parent_span_id"), 256),
                    "depth": _bounded_nonnegative_integer(item.get("depth")),
                    "role": _bounded_text(item.get("role"), 32),
                }
            )
        summaries.append(summary)
    return summaries, total, truncated


def _task_input_resource_summaries(
    values: Any,
) -> tuple[list[Dict[str, Any]], int, bool]:
    items, total, truncated = _bounded_collection(
        values,
        MAX_TASK_INPUT_RESOURCE_SUMMARIES,
        keep_latest=True,
    )
    summaries: list[Dict[str, Any]] = []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        resources, resource_total, resource_truncated = _bounded_collection(
            item.get("resources"),
            MAX_INPUT_RESOURCES_PER_SUMMARY,
        )
        resource_summaries: list[Dict[str, Any]] = []
        for raw_resource in resources:
            resource = raw_resource if isinstance(raw_resource, dict) else {}
            resource_summaries.append(
                {
                    "sequence": _bounded_nonnegative_integer(resource.get("sequence")),
                    "media_type": _bounded_text(resource.get("media_type"), 128),
                    "size_bytes": _bounded_nonnegative_integer(resource.get("size_bytes")),
                    "sha256": _bounded_text(resource.get("sha256"), 128),
                    "dispatch": _bounded_text(resource.get("dispatch"), 64),
                }
            )
        summaries.append(
            {
                "runtime_id": _bounded_text(item.get("runtime_id"), 128),
                "turn_index": _bounded_nonnegative_integer(item.get("turn_index")),
                "request_id": _bounded_text(item.get("request_id"), 256),
                "recorded_at": _bounded_observed_number(item.get("recorded_at")),
                "resources": resource_summaries,
                "resource_count": resource_total,
                "resources_truncated": resource_truncated,
            }
        )
    return summaries, total, truncated


def _previous_collection_total(limits: Dict[str, Any], key: str, observed: int) -> int:
    previous = limits.get(f"{key}_total")
    if isinstance(previous, int) and not isinstance(previous, bool) and previous >= 0:
        return max(observed, min(previous, MAX_USAGE_TOTAL))
    return observed


def _task_forecast_summary(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    baseline = source.get("baseline") if isinstance(source.get("baseline"), dict) else None
    usage = source.get("usage") if isinstance(source.get("usage"), dict) else None
    return {
        "status": _bounded_text(source.get("status"), 64),
        "model": _bounded_text(source.get("model"), 512),
        "context_kind": _bounded_text(source.get("context_kind"), 128),
        "sample_count": _bounded_nonnegative_integer(source.get("sample_count")),
        "max_samples": _bounded_nonnegative_integer(source.get("max_samples")),
        "min_samples": _bounded_nonnegative_integer(source.get("min_samples")),
        "calibration_ratio": _bounded_observed_number(source.get("calibration_ratio")),
        "estimator_version": _bounded_text(source.get("estimator_version"), 128),
        "baseline": normalize_usage(baseline or {}) if baseline is not None else None,
        "usage": normalize_usage(usage or {}) if usage is not None else None,
        "fixed_at": _bounded_observed_number(source.get("fixed_at")),
    }


def _task_usage_summary(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    summary: Dict[str, Any] = _normalize_usage_payload(source)
    summary.update(
        {
            "llm_calls": _bounded_nonnegative_integer(source.get("llm_calls")),
            "cache_hit_calls": _bounded_nonnegative_integer(source.get("cache_hit_calls")),
            "cache_hit_rate": _bounded_observed_number(source.get("cache_hit_rate")) or 0.0,
            "cache_hit_call_rate": _bounded_observed_number(source.get("cache_hit_call_rate"))
            or 0.0,
        }
    )
    return summary


def _bounded_task_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    document_fields = {
        "schema_version",
        "task_id",
        "description",
        "progress",
        "status",
        "submitter",
        "asked_at",
        "started_at",
        "finished_at",
        "turn_finished_at",
        "elapsed_s",
        "updated_at",
        "tools",
        "llm_calls",
        "context_snapshots",
        "input_resources",
        "steps",
        "activity_summary",
        "summary_limits",
        "current_step",
        "usage_totals",
        "forecast",
        "primary_model",
        "context_kind",
        "persona_outcome",
        "job_ids",
        "job_results",
        "session_id",
        "message_id",
        "workspace",
        "path",
        "trace_id",
        "events_path",
        "turn_path",
    }
    unknown_field_count = sum(1 for key in payload if key not in document_fields)
    bounded = {key: value for key, value in payload.items() if key in document_fields}
    bounded["schema_version"] = _bounded_nonnegative_integer(bounded.get("schema_version"))
    for key, limit in (
        ("task_id", 256),
        ("description", 512),
        ("progress", 4096),
        ("status", 64),
        ("submitter", 512),
        ("current_step", 1024),
        ("primary_model", 512),
        ("context_kind", 256),
        ("session_id", 256),
        ("message_id", 256),
        ("trace_id", 256),
        ("path", 1024),
        ("events_path", 1024),
        ("turn_path", 1024),
    ):
        if key in bounded and bounded.get(key) is not None:
            bounded[key] = _bounded_text(bounded.get(key), limit)
    for key in (
        "asked_at",
        "started_at",
        "finished_at",
        "turn_finished_at",
        "elapsed_s",
        "updated_at",
    ):
        if key in bounded:
            bounded[key] = _bounded_observed_number(bounded.get(key))
    bounded["workspace"] = _bounded_mapping(bounded.get("workspace"))
    raw_persona_outcome = bounded.get("persona_outcome")
    persona_outcome = raw_persona_outcome if isinstance(raw_persona_outcome, dict) else {}
    bounded["persona_outcome"] = {
        "outcome": _bounded_text(persona_outcome.get("outcome"), 80),
        "error_code": _bounded_text(persona_outcome.get("error_code"), 120),
    }
    bounded["usage_totals"] = _task_usage_summary(bounded.get("usage_totals"))
    bounded["forecast"] = _task_forecast_summary(bounded.get("forecast"))
    raw_activity = bounded.get("activity_summary")
    activity: Dict[str, Any] = raw_activity if isinstance(raw_activity, dict) else {}
    bounded["activity_summary"] = {
        "provider_total": _bounded_nonnegative_integer(activity.get("provider_total")),
        "provider_retained": _bounded_nonnegative_integer(activity.get("provider_retained")),
        "provider_dropped": _bounded_nonnegative_integer(activity.get("provider_dropped")),
        "truncated": bool(activity.get("truncated")),
    }
    raw_tools = bounded.get("tools")
    tool_source = raw_tools if isinstance(raw_tools, (list, tuple)) else []
    observed_tool_total = len(tool_source)
    tool_values = [
        item if isinstance(item, dict) else {} for item in tool_source[-MAX_TASK_TOOL_SUMMARIES:]
    ]
    bounded["tools"] = _task_tool_summaries(tool_values)
    raw_steps = bounded.get("steps")
    step_source = raw_steps if isinstance(raw_steps, (list, tuple)) else []
    observed_step_total = len(step_source)
    step_values = [
        item if isinstance(item, dict) else {} for item in step_source[-MAX_TASK_STEP_SUMMARIES:]
    ]
    bounded["steps"] = _task_step_summaries(step_values)
    raw_job_id_values, raw_job_id_total, invalid_job_ids = _bounded_collection(
        bounded.get("job_ids"),
        MAX_JOB_RESULT_SUMMARIES,
    )
    raw_job_ids = [str(value) for value in raw_job_id_values]
    raw_job_result_values, raw_job_result_total, invalid_job_results = _bounded_collection(
        bounded.get("job_results"),
        MAX_JOB_RESULT_SUMMARIES,
    )
    raw_job_results = list(raw_job_result_values)
    bounded["job_ids"] = [_bounded_text(value, 256) for value in raw_job_ids]
    bounded["job_results"] = _bounded_job_results(raw_job_results)
    limits = _bounded_mapping(bounded.get("summary_limits"))
    if unknown_field_count:
        limits["unknown_fields_omitted"] = unknown_field_count
        limits["truncated"] = True
    llm_calls, llm_call_total, llm_calls_truncated = _task_llm_call_summaries(
        bounded.get("llm_calls")
    )
    contexts, context_total, contexts_truncated = _task_context_snapshot_summaries(
        bounded.get("context_snapshots")
    )
    input_resources, input_resource_total, input_resources_truncated = (
        _task_input_resource_summaries(bounded.get("input_resources"))
    )
    bounded["llm_calls"] = llm_calls
    bounded["context_snapshots"] = contexts
    bounded["input_resources"] = input_resources
    llm_call_total = _previous_collection_total(limits, "llm_calls", llm_call_total)
    context_total = _previous_collection_total(
        limits,
        "context_snapshots",
        context_total,
    )
    input_resource_total = _previous_collection_total(
        limits,
        "input_resources",
        input_resource_total,
    )
    tool_total = _previous_collection_total(limits, "tools", observed_tool_total)
    step_total = _previous_collection_total(limits, "steps", observed_step_total)
    job_truncated = (
        invalid_job_ids
        or invalid_job_results
        or raw_job_id_total > len(bounded["job_ids"])
        or raw_job_result_total > len(bounded["job_results"])
        or any(item.get("payload_truncated") for item in bounded["job_results"])
    )
    limits.update(
        {
            "job_ids_total": raw_job_id_total,
            "job_ids_retained": len(bounded["job_ids"]),
            "job_results_total": raw_job_result_total,
            "job_results_retained": len(bounded["job_results"]),
            "job_results_truncated": job_truncated,
            "tools_total": tool_total,
            "tools_retained": len(bounded["tools"]),
            "steps_total": step_total,
            "steps_retained": len(bounded["steps"]),
            "llm_calls_total": llm_call_total,
            "llm_calls_retained": len(llm_calls),
            "llm_calls_truncated": llm_calls_truncated or llm_call_total > len(llm_calls),
            "context_snapshots_total": context_total,
            "context_snapshots_retained": len(contexts),
            "context_snapshots_truncated": contexts_truncated or context_total > len(contexts),
            "input_resources_total": input_resource_total,
            "input_resources_retained": len(input_resources),
            "input_resources_truncated": input_resources_truncated
            or input_resource_total > len(input_resources),
        }
    )
    limits["truncated"] = bool(limits.get("truncated")) or any(
        (
            job_truncated,
            tool_total > len(bounded["tools"]),
            step_total > len(bounded["steps"]),
            limits["llm_calls_truncated"],
            limits["context_snapshots_truncated"],
            limits["input_resources_truncated"],
        )
    )
    bounded["summary_limits"] = limits
    encoded = _private_json_bytes(bounded)
    if len(encoded) <= MAX_TASK_SUMMARY_BYTES:
        return bounded

    original_bytes = len(encoded)
    original_sha256 = hashlib.sha256(encoded).hexdigest()
    for key in ("tools", "steps"):
        value = bounded.get(key)
        limits[f"{key}_payload_count"] = len(value) if isinstance(value, list) else 0
        bounded[key] = []
    minimal_contexts, _, _ = _task_context_snapshot_summaries(
        contexts,
        minimal=True,
    )
    bounded["context_snapshots"] = minimal_contexts
    limits["context_snapshots_retained"] = len(minimal_contexts)
    limits["context_snapshots_minimal"] = True
    limits.update(
        {
            "payload_truncated": True,
            "payload_original_bytes": original_bytes,
            "payload_sha256": original_sha256,
            "truncated": True,
        }
    )
    if len(_private_json_bytes(bounded)) <= MAX_TASK_SUMMARY_BYTES:
        return bounded

    bounded["job_results"] = _bounded_job_results(raw_job_results, compact=True)
    limits["job_result_content_omitted"] = True
    bounded["llm_calls"] = []
    bounded["input_resources"] = []
    limits["llm_calls_retained"] = 0
    limits["llm_calls_truncated"] = llm_call_total > 0
    limits["input_resources_retained"] = 0
    limits["input_resources_truncated"] = input_resource_total > 0
    if len(_private_json_bytes(bounded)) > MAX_TASK_SUMMARY_BYTES:
        raise ValueError("bounded task summary exceeds the hard size limit")
    return bounded


def _bounded_turn_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    bounded = dict(payload)
    omissions: list[str] = []
    for key, limit in (
        ("task_id", 256),
        ("session_id", 256),
        ("message_id", 256),
        ("stop_reason", 512),
        ("user_text", 1024 * 1024),
        ("final_text", 1024 * 1024),
        ("error", 1024 * 1024),
    ):
        if key not in bounded or bounded.get(key) is None:
            continue
        value, truncated = _bounded_string(bounded.get(key), limit)
        bounded[key] = value
        if truncated:
            omissions.append(key)
    resource_values, resource_total, invalid_resources = _bounded_collection(
        bounded.get("produced_resources"),
        1000,
    )
    raw_resources = [value for value in resource_values if isinstance(value, str)]
    bounded["produced_resources"] = [_bounded_text(value, 1024) for value in raw_resources]
    if invalid_resources or resource_total > len(bounded["produced_resources"]):
        omissions.append("produced_resources")
    raw_job_results, job_result_total, invalid_job_results = _bounded_collection(
        bounded.get("job_results"),
        MAX_JOB_RESULT_SUMMARIES,
    )
    bounded["job_results"] = _bounded_job_results(raw_job_results)
    if (
        invalid_job_results
        or job_result_total > len(bounded["job_results"])
        or any(item.get("payload_truncated") for item in bounded["job_results"])
    ):
        omissions.append("job_results")
    if omissions:
        bounded["payload_truncated"] = True
        bounded["omitted_fields"] = sorted(set(omissions))
        bounded["payload_sha256"] = _payload_digest(payload)
    if len(_private_json_bytes(bounded)) <= MAX_TASK_SUMMARY_BYTES:
        return bounded
    bounded["user_text"] = _bounded_text(bounded.get("user_text"), 256 * 1024)
    bounded["final_text"] = _bounded_text(bounded.get("final_text"), 256 * 1024)
    bounded["error"] = _bounded_text(bounded.get("error"), 256 * 1024)
    bounded["job_results"] = _bounded_job_results(raw_job_results, compact=True)
    bounded["payload_truncated"] = True
    bounded["omitted_fields"] = sorted(
        set(list(bounded.get("omitted_fields") or []) + ["large_content"])
    )
    if len(_private_json_bytes(bounded)) > MAX_TASK_SUMMARY_BYTES:
        raise ValueError("bounded turn artifact exceeds the hard size limit")
    return bounded


def _bounded_task_or_turn_document(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if name == TASK_FILENAME:
        return _bounded_task_document(payload)
    if name == TURN_FILENAME:
        return _bounded_turn_document(payload)
    return payload


def _job_result_summary(
    job_id: str,
    result: Dict[str, Any],
    *,
    omit_free_text: bool = False,
) -> Dict[str, Any]:
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    raw_summary = str(result.get("summary") or "")
    raw_error = str(result.get("error") or "")
    summary = _bounded_job_result(
        {
            "job_id": job_id,
            "ok": bool(result.get("ok")),
            "status": "succeeded" if result.get("ok") else "failed",
            "stage": str(
                details.get("failed_stage")
                or result.get("stage")
                or ("succeeded" if result.get("ok") else "failed")
            ),
            "error_code": str(result.get("error_code") or ""),
            "summary": "" if omit_free_text else raw_summary,
            "error": "" if omit_free_text else raw_error,
            "outputs": [str(item) for item in result.get("outputs") or [] if isinstance(item, str)],
            "finished_at": result.get("finished_at"),
        }
    )
    omitted_fields = [
        field
        for field, value in (("summary", raw_summary), ("error", raw_error))
        if omit_free_text and value
    ]
    if omitted_fields:
        summary["payload_truncated"] = True
        summary["omitted_fields"] = omitted_fields
        summary["payload_sha256"] = _payload_digest(
            {
                "job_id": job_id,
                "ok": bool(result.get("ok")),
                "finished_at": result.get("finished_at"),
            }
        )
    return summary


def _delegated_progress(results: List[Dict[str, Any]], expected: int) -> str:
    completed = len(results)
    failed = sum(1 for item in results if not item.get("ok"))
    if completed < expected:
        return f"Background child jobs completed: {completed}/{expected}."
    if failed:
        return f"{failed} background child job(s) failed."
    return f"All {expected} background child job(s) completed."


def _task_tool_summaries(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for item in tools[-MAX_TASK_TOOL_SUMMARIES:]:
        summaries.append(
            {
                "name": _bounded_text(item.get("name"), 256),
                "kind": _bounded_text(item.get("kind"), 64),
                "status": _bounded_text(item.get("status"), 64),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "elapsed_s": item.get("elapsed_s"),
                "summary": _bounded_text(item.get("summary"), 2000),
                "error": (
                    _bounded_text(item.get("error"), 2000)
                    if item.get("error") is not None
                    else None
                ),
                "span_id": (
                    _bounded_text(item.get("span_id"), 256)
                    if item.get("span_id") is not None
                    else None
                ),
                "parent_span_id": (
                    _bounded_text(item.get("parent_span_id"), 256)
                    if item.get("parent_span_id") is not None
                    else None
                ),
                "depth": item.get("depth"),
            }
        )
    return summaries


def _task_event_bytes(event: Dict[str, Any]) -> bytes:
    return (
        json.dumps(
            event,
            ensure_ascii=False,
            default=str,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _bounded_event_payload(value: Any) -> Dict[str, Any]:
    canonical = _json_bytes(value)
    retained: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key in (
            "name",
            "kind",
            "status",
            "ok",
            "trace_id",
            "span_id",
            "parent_span_id",
            "step_id",
            "job_id",
            "depth",
            "iteration",
            "model",
            "finish_reason",
            "started_at",
            "finished_at",
            "recorded_at",
        ):
            raw = value.get(key)
            if raw is None or isinstance(raw, (bool, int, float)):
                retained[key] = raw
            elif isinstance(raw, str):
                retained[key] = _bounded_text(raw, 512)
        for key in ("summary", "error", "message"):
            if key in value:
                retained[key] = _bounded_text(value.get(key), 2000)
    retained.update(
        {
            "payload_truncated": True,
            "original_bytes": len(canonical),
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "top_level_item_count": len(value) if isinstance(value, (dict, list)) else 1,
        }
    )
    return retained


def _task_step_summaries(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for item in steps[-MAX_TASK_STEP_SUMMARIES:]:
        summaries.append(
            {
                "step_id": _bounded_text(item.get("step_id"), 256),
                "type": _bounded_text(item.get("type"), 64),
                "parent_step_id": (
                    _bounded_text(item.get("parent_step_id"), 256)
                    if item.get("parent_step_id") is not None
                    else None
                ),
                "depth": item.get("depth"),
                "status": _bounded_text(item.get("status"), 64),
                "title": _bounded_text(item.get("title"), 512),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "elapsed_s": item.get("elapsed_s"),
                "summary": _bounded_text(item.get("summary"), 2000),
                "error": (
                    _bounded_text(item.get("error"), 2000)
                    if item.get("error") is not None
                    else None
                ),
                "metadata": _bounded_mapping(item.get("metadata")),
                "estimated_usage": normalize_usage(item.get("estimated_usage")),
                "actual_usage": normalize_usage(item.get("actual_usage")),
                "inclusive_usage": normalize_usage(item.get("inclusive_usage")),
                "raw_event_types": [
                    _bounded_text(value, 128)
                    for value in list(item.get("raw_event_types") or [])[:20]
                ],
            }
        )
    return summaries


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def _message_manifest(messages: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in list(messages or [])[:200]:
        item = raw if isinstance(raw, dict) else {"content": raw}
        content = item.get("content")
        serialized = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        serialized_message = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        reported_content_chars = len(serialized)
        if isinstance(content, str):
            for match in _TRUNCATED_ORIGINAL_CHARS_RE.finditer(content):
                try:
                    reported_content_chars = max(
                        reported_content_chars,
                        int(match.group(1)),
                    )
                except ValueError:
                    continue
        reported_message_chars = len(serialized_message) + max(
            0,
            reported_content_chars - len(serialized),
        )
        out.append(
            {
                "role": _bounded_text(item.get("role"), 256),
                "name": _bounded_text(item.get("name"), 256),
                "char_count": reported_message_chars,
                "message_sha256": hashlib.sha256(serialized_message.encode("utf-8")).hexdigest(),
                "content_char_count": reported_content_chars,
                "content_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "content_preview": serialized[:1024],
            }
        )
    return out


def _tool_schema_manifest(schemas: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in list(schemas or [])[:200]:
        item = raw if isinstance(raw, dict) else {"schema": raw}
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = function.get("name") or item.get("name") or ""
        serialized = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        out.append(
            {
                "name": _bounded_text(name, 256),
                "char_count": len(serialized),
                "schema_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            }
        )
    return out


def _truncated_context_payload(
    payload: Dict[str, Any],
    *,
    original_bytes: int,
    content_sha256: str,
) -> Dict[str, Any]:
    bounded: Dict[str, Any] = {
        "schema_version": payload.get("schema_version", 1),
        "task_id": _bounded_text(payload.get("task_id"), 256),
        "snapshot_id": _bounded_text(payload.get("snapshot_id"), 256),
        "captured_at": payload.get("captured_at"),
        "runtime_id": _bounded_text(payload.get("runtime_id"), 256),
        "model": _bounded_text(payload.get("model"), 1024),
        "iteration": payload.get("iteration"),
        "coverage": _bounded_text(payload.get("coverage"), 256),
        "omitted": [_bounded_text(item, 512) for item in list(payload.get("omitted") or [])[:200]],
        "context_kind": _bounded_text(payload.get("context_kind"), 256),
        "trace_id": _bounded_text(payload.get("trace_id"), 256),
        "span_id": _bounded_text(payload.get("span_id"), 256),
        "parent_span_id": _bounded_text(payload.get("parent_span_id"), 256),
        "depth": payload.get("depth"),
        "estimated_tokens": payload.get("estimated_tokens"),
        "model_selection": _bounded_mapping(payload.get("model_selection")),
        "session_messages": _message_manifest(payload.get("session_messages")),
        "effective_messages": _message_manifest(payload.get("effective_messages")),
        "tool_schemas": _tool_schema_manifest(payload.get("tool_schemas")),
        "resources": _resource_manifest(payload.get("resources")),
        "capture_status": "truncated",
        "truncated": True,
        "original_bytes": original_bytes,
        "content_sha256": content_sha256,
        "sanitization": payload.get("sanitization"),
    }
    _set_stored_bytes(bounded)
    if len(_json_bytes(bounded)) > MAX_CONTEXT_ARTIFACT_BYTES:
        for key in ("session_messages", "effective_messages"):
            for item in bounded.get(key) or []:
                if isinstance(item, dict):
                    item.pop("content_preview", None)
        _set_stored_bytes(bounded)
    if len(_json_bytes(bounded)) > MAX_CONTEXT_ARTIFACT_BYTES:
        bounded["session_messages"] = []
        bounded["effective_messages"] = []
        bounded["tool_schemas"] = []
        bounded["resources"] = []
        bounded["truncation_reason"] = "artifact_size_limit"
        _set_stored_bytes(bounded)
    if len(_json_bytes(bounded)) > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("truncated context artifact exceeds the hard size limit")
    return bounded


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _bounded_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:100]:
        key = _bounded_text(raw_key, 256)
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            out[key] = raw_value
        else:
            out[key] = _bounded_text(raw_value, 1024)
    return out


def _resource_manifest(resources: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in list(resources or [])[:200]:
        item = raw if isinstance(raw, dict) else {}
        out.append(
            {
                "sequence": item.get("sequence"),
                "media_type": _bounded_text(item.get("media_type"), 256),
                "size_bytes": item.get("size_bytes"),
                "sha256": _bounded_text(item.get("sha256"), 128),
            }
        )
    return out


def _set_stored_bytes(payload: Dict[str, Any]) -> None:
    for _ in range(4):
        stored_bytes = len(_json_bytes(payload))
        if payload.get("stored_bytes") == stored_bytes:
            return
        payload["stored_bytes"] = stored_bytes


def _normalize_usage_payload(usage: Dict[str, Any]) -> Dict[str, int]:
    normalized = normalize_usage(usage)
    return {
        key: normalized[key]
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    }


_MAX_USAGE_TOKEN_COUNT = (1 << 63) - 1


_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _nonnegative_usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        normalized = value
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(numeric):
            return 0
        normalized = int(numeric)
    if normalized < 0 or normalized > _MAX_USAGE_TOKEN_COUNT:
        return 0
    return normalized


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    source = usage or {}
    normalized: dict[str, int] = {}
    for key in _USAGE_KEYS:
        normalized[key] = _nonnegative_usage_int(source.get(key, 0))
    prompt = normalized["prompt_tokens"]
    cached = min(
        prompt,
        max(normalized["cached_tokens"], normalized["cache_read_tokens"]),
    )
    normalized["cached_tokens"] = cached
    normalized["cache_read_tokens"] = min(prompt, normalized["cache_read_tokens"])
    normalized["non_cached_input_tokens"] = max(0, prompt - cached)
    normalized["input_tokens"] = prompt
    normalized["output_tokens"] = normalized["completion_tokens"]
    return normalized
