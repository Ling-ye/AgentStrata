"""Safe Agent-event projection for Evaluation artifacts.

Evaluation records must never retain the full model-boundary context carried by
``ContextSnapshotPrepared``.  The normal task recorder persists that context
through its separately bounded and redacted artifact path; Evaluation only
needs correlation and coverage metadata.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from chatcopilot.contracts.agent import ContextSnapshotPrepared


_MODEL_SELECTION_FIELDS = (
    "lane",
    "provider",
    "model",
    "reasoning_effort",
    "scope",
    "source",
    "profile",
)
_MAX_OBSERVABILITY_COUNT = (1 << 63) - 1
_MAX_OBSERVABILITY_TEXT_CHARS = 512
_MAX_OMITTED_ITEMS = 64


def project_evaluation_event(event: object) -> dict[str, Any]:
    """Return an Evaluation-safe event mapping.

    Existing events retain their historical ``asdict`` projection.  Context
    snapshots are deliberately projected without recursively copying any
    message, tool-schema, or resource body.
    """

    if isinstance(event, ContextSnapshotPrepared):
        return _project_context_snapshot(event)
    if isinstance(event, Mapping):
        if str(event.get("type") or "") == ContextSnapshotPrepared.__name__:
            return _project_context_snapshot(event)
        return dict(event)
    if is_dataclass(event):
        value = asdict(event)  # type: ignore[arg-type]
        value["type"] = type(event).__name__
        return value
    return {"type": type(event).__name__}


def _project_context_snapshot(
    event: ContextSnapshotPrepared | Mapping[str, Any],
) -> dict[str, Any]:
    get = _event_getter(event)
    session_messages = get("session_messages")
    effective_messages = get("effective_messages")
    tool_schemas = get("tool_schemas")
    resources = get("resources")
    return {
        "type": ContextSnapshotPrepared.__name__,
        "snapshot_id": _text(get("snapshot_id")),
        "backend": _text(get("backend")),
        "model": _text(get("model")),
        "iteration": _nonnegative_int(get("iteration")),
        "coverage": _text(get("coverage")),
        "omitted": _safe_omitted(get("omitted")),
        "context_kind": _text(get("context_kind")),
        "trace_id": _optional_text(get("trace_id")),
        "span_id": _optional_text(get("span_id")),
        "parent_span_id": _optional_text(get("parent_span_id")),
        "depth": _nonnegative_int(get("depth")),
        "estimated_tokens": _nonnegative_int(get("estimated_tokens")),
        "model_selection": _safe_model_selection(get("model_selection")),
        "message_count": len(_sequence(session_messages)),
        "effective_message_count": len(_sequence(effective_messages)),
        "tool_schema_count": len(_sequence(tool_schemas)),
        "resource_count": len(_sequence(resources)),
        "private_reasoning_omission_count": _nonnegative_int(
            get("private_reasoning_omission_count")
        ),
        "resource_path_omission_count": _nonnegative_int(
            get("resource_path_omission_count")
        ),
    }


def _event_getter(event: object):
    if isinstance(event, Mapping):
        return event.get
    return lambda key: getattr(event, key, None)


def _sequence(value: Any) -> tuple[Any, ...] | list[Any]:
    return value if isinstance(value, (tuple, list)) else ()


def _optional_text(value: Any) -> str | None:
    return value[:_MAX_OBSERVABILITY_TEXT_CHARS] if isinstance(value, str) else None


def _text(value: Any) -> str:
    return value[:_MAX_OBSERVABILITY_TEXT_CHARS] if isinstance(value, str) else ""


def _safe_omitted(value: Any) -> tuple[str, ...]:
    return tuple(
        item[:_MAX_OBSERVABILITY_TEXT_CHARS]
        for item in _sequence(value)[:_MAX_OMITTED_ITEMS]
        if isinstance(item, str)
    )


def _nonnegative_int(value: Any) -> int:
    try:
        return min(_MAX_OBSERVABILITY_COUNT, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_model_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item[:_MAX_OBSERVABILITY_TEXT_CHARS]
        for key in _MODEL_SELECTION_FIELDS
        if key in value and isinstance((item := value.get(key)), str)
    }


__all__ = ["project_evaluation_event"]
