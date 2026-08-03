"""Lightweight in-process tracing for the agent layer.

The agent layer must stay platform-neutral, so we do not pull in OpenTelemetry.
Instead each turn gets a ``trace_id`` and every tool call / subagent boundary
gets a ``span_id`` linked by ``parent_span_id``. Spans are surfaced through the
existing :class:`AgentEvent` stream (tool events carry trace fields, plus
``SpanStarted`` / ``SpanFinished`` for the subagent boundary). Middleware records
them; nothing here knows about ACP / platforms.

A :class:`TraceContext` is published on a contextvar (same pattern as
``tools/file_delivery.py``) so that a nested subagent — executed synchronously
inside a tool handler on the same thread — can discover the parent span and the
event sink to re-parent its own spans onto the main trace tree.
"""

from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass
from typing import Optional

from chatcopilot.agent.protocol import EventSink


@dataclass(frozen=True)
class TraceContext:
    """The currently-executing span, exposed to nested execution."""

    trace_id: str
    span_id: str
    depth: int
    sink: Optional[EventSink] = None


def new_trace_id() -> str:
    return "trace_" + uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return "span_" + uuid.uuid4().hex[:12]


_CURRENT_TRACE: contextvars.ContextVar[Optional[TraceContext]] = contextvars.ContextVar(
    "chatcopilot_current_trace", default=None
)


def current_trace() -> Optional[TraceContext]:
    return _CURRENT_TRACE.get()


def set_trace(ctx: Optional[TraceContext]) -> contextvars.Token:
    return _CURRENT_TRACE.set(ctx)


def reset_trace(token: contextvars.Token) -> None:
    _CURRENT_TRACE.reset(token)


__all__ = [
    "TraceContext",
    "current_trace",
    "new_span_id",
    "new_trace_id",
    "reset_trace",
    "set_trace",
]
