"""Agent protocol contracts shared by agent and middleware."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, TypeAlias, Union


AgentStopReason: TypeAlias = Literal[
    "end_turn",
    "tool_failure_cap",
    "iteration_cap",
    "tool_call_cap",
    "timeout_cap",
    "llm_error",
]


@dataclass(frozen=True)
class ResourceRef:
    name: str
    path: str
    kind: Literal["file", "directory", "url"] = "file"
    schema: Mapping[str, Any] | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class AgentTask:
    text: str
    resources: tuple[ResourceRef, ...] = ()
    turn_context: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class FinalText:
    text: str


@dataclass(frozen=True)
class ToolStarted:
    name: str
    arguments: Mapping[str, Any]
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0
    started_at: float | None = None


@dataclass(frozen=True)
class ToolFinished:
    name: str
    ok: bool
    summary: str
    error: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0
    data: Mapping[str, Any] | None = None
    finished_at: float | None = None


@dataclass(frozen=True)
class SpanStarted:
    name: str
    kind: str
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0


@dataclass(frozen=True)
class SpanFinished:
    name: str
    kind: str
    ok: bool
    summary: str = ""
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class LlmCallStarted:
    model: str
    iteration: int
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0
    input_message_count: int = 0
    input_estimated_tokens: int = 0
    system_estimated_tokens: int = 0
    tool_schema_count: int = 0
    tool_schema_estimated_tokens: int = 0
    estimator_version: str = ""
    context_kind: str = ""
    context_snapshot_id: str = ""
    backend: str = ""


@dataclass(frozen=True)
class LlmCallFinished:
    model: str
    iteration: int
    finish_reason: str = ""
    usage: Mapping[str, Any] | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0
    input_message_count: int = 0
    input_estimated_tokens: int = 0
    system_estimated_tokens: int = 0
    tool_schema_count: int = 0
    tool_schema_estimated_tokens: int = 0
    estimator_version: str = ""
    context_kind: str = ""
    context_snapshot_id: str = ""
    ok: bool = True
    backend: str = ""


@dataclass(frozen=True)
class InputResourceReceipt:
    """Path-free identity for one image actually included in a backend request."""

    sequence: int
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class InputResourcesDispatched:
    """Evidence emitted only after the backend request carrying images returned."""

    backend: str
    turn_index: int
    request_id: str
    resources: tuple[InputResourceReceipt, ...]


@dataclass(frozen=True)
class ContextSnapshotPrepared:
    """AgentStrata-visible context captured at one model request boundary.

    ``coverage`` describes what the backend can prove. Native and LangGraph
    use ``exact_model_input`` for text-only calls and ``partial`` when private
    reasoning or binary resource payloads are intentionally omitted. Adapters
    around provider-managed sessions use ``adapter_visible`` and enumerate
    opaque state in ``omitted``.
    """

    snapshot_id: str
    backend: str
    model: str
    iteration: int
    session_messages: tuple[Mapping[str, Any], ...]
    effective_messages: tuple[Mapping[str, Any], ...]
    tool_schemas: tuple[Mapping[str, Any], ...] = ()
    resources: tuple[InputResourceReceipt, ...] = ()
    coverage: str = "exact_model_input"
    omitted: tuple[str, ...] = ()
    context_kind: str = ""
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    depth: int = 0
    estimated_tokens: int = 0
    model_selection: Mapping[str, Any] | None = None
    private_reasoning_omission_count: int = 0
    resource_path_omission_count: int = 0


@dataclass(frozen=True)
class TopicDecisionMade:
    decision: str
    context_kind: str
    confidence: float
    reason: str
    source: str
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_s: float | None = None


@dataclass(frozen=True)
class TurnError:
    code: str
    message: str


AgentEvent = Union[
    TextDelta,
    FinalText,
    ToolStarted,
    ToolFinished,
    SpanStarted,
    SpanFinished,
    LlmCallStarted,
    LlmCallFinished,
    ContextSnapshotPrepared,
    InputResourcesDispatched,
    TopicDecisionMade,
    TurnError,
]
EventSink = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class DeferredLifecycleIntent:
    name: Literal["finalize_self_update"]
    arguments: Mapping[str, Any]
    source: Literal["main", "subagent"]
    requires_final_delivery: bool = True


@dataclass(frozen=True)
class AgentResult:
    final_text: str
    stop_reason: AgentStopReason
    produced_resources: tuple[ResourceRef, ...] = ()
    message_count: int = 0
    response_integrity: Any = None
    lifecycle_intents: tuple[DeferredLifecycleIntent, ...] = ()


__all__ = [
    "AgentTask",
    "ResourceRef",
    "AgentEvent",
    "AgentResult",
    "AgentStopReason",
    "DeferredLifecycleIntent",
    "EventSink",
    "TextDelta",
    "FinalText",
    "ToolStarted",
    "ToolFinished",
    "SpanStarted",
    "SpanFinished",
    "LlmCallStarted",
    "LlmCallFinished",
    "ContextSnapshotPrepared",
    "InputResourceReceipt",
    "InputResourcesDispatched",
    "TopicDecisionMade",
    "TurnError",
]
