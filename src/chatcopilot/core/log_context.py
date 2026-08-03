"""Context-local correlation fields for runtime logs."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator


_FIELDS = ("task_id", "trace_id", "session_id", "job_id")
_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "chatcopilot_log_context", default={}
)


def current_log_context() -> dict[str, str]:
    return dict(_CONTEXT.get())


def push_log_context(**fields: object) -> contextvars.Token[dict[str, str]]:
    merged = current_log_context()
    for key in _FIELDS:
        value = str(fields.get(key) or "").strip()
        if value:
            merged[key] = value
    return _CONTEXT.set(merged)


def pop_log_context(token: contextvars.Token[dict[str, str]]) -> None:
    _CONTEXT.reset(token)


@contextmanager
def bind_log_context(**fields: object) -> Iterator[None]:
    token = push_log_context(**fields)
    try:
        yield
    finally:
        pop_log_context(token)


__all__ = [
    "bind_log_context",
    "current_log_context",
    "pop_log_context",
    "push_log_context",
]
