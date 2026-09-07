"""Context-local diagnostics; callbacks cannot influence execution or authorization."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

ObservationSink = Callable[[str, dict[str, Any]], None]
_SINK: ContextVar[ObservationSink | None] = ContextVar("operation_observation", default=None)
_PHASE: ContextVar[str] = ContextVar("permission_observation_phase", default="visibility")


def observe(kind: str, **data: Any) -> None:
    sink = _SINK.get()
    if sink is not None:
        try:
            sink(kind, data)
        except Exception:
            pass


@contextmanager
def observation_scope(sink: ObservationSink) -> Iterator[None]:
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


@contextmanager
def permission_phase(phase: str) -> Iterator[None]:
    token = _PHASE.set(phase)
    try:
        yield
    finally:
        _PHASE.reset(token)


def current_permission_phase() -> str:
    return _PHASE.get()
