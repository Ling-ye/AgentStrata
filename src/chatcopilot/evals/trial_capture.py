"""Bounded execution observations; never a resumable Trial checkpoint."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import json
import threading
from dataclasses import replace
from functools import wraps
from typing import Any, Callable, Iterator

from chatcopilot.evals.redaction import collect_env_secrets, redact_payload

_sink: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "evaluation_capture_sink", default=None
)
_current: ContextVar[dict[str, Any] | None] = ContextVar("evaluation_capture", default=None)
_write_lock: ContextVar[Any] = ContextVar("evaluation_capture_write_lock", default=None)
_TEXT_LIMIT = 128 * 1024
_BYTE_LIMIT = 512 * 1024


@contextmanager
def capture(sink: Callable[[dict[str, Any]], None] | None = None) -> Iterator[dict[str, Any]]:
    value: dict[str, Any] = {"turns": [], "state": "not_recorded"}
    lock_token = _write_lock.set(_write_lock.get() or threading.RLock())
    token = _current.set(value)
    sink_token = _sink.set(sink if sink is not None else _sink.get())
    try:
        yield value
    finally:
        _current.reset(token)
        _sink.reset(sink_token)
        _write_lock.reset(lock_token)


def _serialized_capture(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        # Parallel Agent callbacks share one IPC connection. Publish whole
        # frames under the same lock, including updates to their timing snapshot.
        with _write_lock.get() or nullcontext():
            return function(*args, **kwargs)

    return wrapped


@_serialized_capture
def record_turn(value: dict[str, Any]) -> None:
    current = _current.get()
    if current is None:
        return
    turn = dict(value)
    turn["state"] = "recorded"
    for key in ("input", "final_text"):
        text = str(turn.get(key, ""))
        if len(text) > _TEXT_LIMIT:
            turn["state"] = "truncated"
        turn[key] = text[:_TEXT_LIMIT]
    turns = current["turns"]
    identity = (turn.get("conversation_id"), turn.get("turn_index"))
    index = next(
        (
            i
            for i, item in enumerate(turns)
            if (item.get("conversation_id"), item.get("turn_index")) == identity
        ),
        None,
    )
    candidate = list(turns)
    if index is None:
        candidate.append(turn)
    else:
        candidate[index] = turn
    safe = redact_payload({"turns": candidate}, secrets=collect_env_secrets())
    if len(json.dumps(safe, ensure_ascii=False).encode()) > _BYTE_LIMIT:
        current["state"] = "truncated"
        marker = {
            "conversation_id": turn.get("conversation_id"),
            "turn_index": turn.get("turn_index"),
            "completed": turn.get("completed", False),
            "input": "",
            "final_text": "",
            "state": "truncated",
        }
        if index is None:
            current["turns"].append(marker)
        else:
            current["turns"][index] = marker
    else:
        current["turns"] = safe["turns"]
        current["state"] = (
            "truncated" if any(t.get("state") == "truncated" for t in candidate) else "recorded"
        )
    sample_execution(force=True, publish=False)
    sink = _sink.get()
    if sink is not None:
        sink(current)


@_serialized_capture
def set_phase(phase: str) -> None:
    current = _current.get()
    if current is not None:
        current["phase"] = phase
        sink = _sink.get()
        if sink is not None:
            sink(current)


def capture_case(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with capture() as observed:
            result = function(*args, **kwargs)
            turns = observed["turns"]
            final = turns[-1] if turns and turns[-1].get("completed") else {}
            truncated = False

            def bounded(value: Any) -> Any:
                nonlocal truncated
                if isinstance(value, str):
                    truncated = truncated or len(value) > _TEXT_LIMIT
                    return value[:_TEXT_LIMIT]
                if isinstance(value, dict):
                    return {key: bounded(item) for key, item in value.items()}
                if isinstance(value, (list, tuple)):
                    return [bounded(item) for item in value]
                return value

            text = bounded(result.final_text or final.get("final_text", ""))
            metadata = bounded(result.metadata)
            if truncated:
                observed["state"] = "truncated"
            return replace(
                result,
                final_text=text,
                stop_reason=result.stop_reason or final.get("stop_reason", ""),
                metadata={**metadata, **timing_metadata(observed), "execution": observed},
            )

    return wrapped


@_serialized_capture
def record_environment(lease: dict[str, Any]) -> None:
    """Publish a host-created lease before any Agent receives environment input."""
    current = _current.get()
    if current is None:
        raise ValueError("benchmark environment requires a managed capture scope")
    current["environment"] = dict(lease)
    sink = _sink.get()
    if sink is not None:
        sink(current)


@contextmanager
def execution_phase(kind: str) -> Iterator[None]:
    """Measure the actual driver interval, separately from preparation and judging."""
    import time

    current = _current.get()
    started = time.monotonic()
    with _write_lock.get() or nullcontext():
        if current is not None:
            current["timing"] = {"kind": kind, "state": "running", "seconds": 0.0}
            current["phase"] = "executing"
    token = _execution_started.set(started)
    try:
        sample_execution(force=True)
        yield
    finally:
        _execution_started.reset(token)
        with _write_lock.get() or nullcontext():
            if current is not None:
                current["timing"] = {
                    "kind": kind,
                    "state": "complete",
                    "seconds": max(0.0, time.monotonic() - started),
                }
                current["phase"] = "executed"
                sink = _sink.get()
                if sink is not None:
                    sink(current)


_execution_started: ContextVar[float | None] = ContextVar(
    "evaluation_execution_started", default=None
)


@_serialized_capture
def sample_execution(*, force: bool = False, publish: bool = True) -> None:
    """Publish a bounded lower bound while a driver is running; no timer thread."""
    import time

    current, started = _current.get(), _execution_started.get()
    if current is None or started is None or current.get("timing", {}).get("state") != "running":
        return
    elapsed = max(0.0, time.monotonic() - started)
    if not force and elapsed - current["timing"]["seconds"] < 1.0:
        return
    current["timing"]["seconds"] = elapsed
    sink = _sink.get()
    if publish and sink is not None:
        sink(current)


def timing_metadata(execution: dict[str, Any]) -> dict[str, float]:
    import math

    timing = execution.get("timing")
    if not isinstance(timing, dict) or timing.get("state") != "complete":
        return {}
    seconds = timing.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return {}
    if not math.isfinite(seconds) or seconds < 0:
        return {}
    key = {"agent": "agent_duration_seconds", "runtime": "runtime_duration_seconds"}.get(
        str(timing.get("kind"))
    )
    return {key: float(seconds)} if key else {}


def execution_snapshot() -> dict[str, Any]:
    """Copy the current bounded observation for the scorer, without creating evidence."""
    from copy import deepcopy

    with _write_lock.get() or nullcontext():
        return deepcopy(_current.get() or {"state": "not_recorded", "turns": []})
