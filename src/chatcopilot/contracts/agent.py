"""Agent protocol contracts shared by agent and middleware."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Union


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
    system_appendix: str | None = None
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
    stop_reason: Literal[
        "end_turn",
        "tool_failure_cap",
        "iteration_cap",
        "tool_call_cap",
        "timeout_cap",
        "llm_error",
    ]
    produced_resources: tuple[ResourceRef, ...] = ()
    message_count: int = 0
    quality_gate: Any = None
    lifecycle_intents: tuple[DeferredLifecycleIntent, ...] = ()


__all__ = [
    "AgentTask",
    "ResourceRef",
    "AgentEvent",
    "AgentResult",
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
    "TopicDecisionMade",
    "TurnError",
]
