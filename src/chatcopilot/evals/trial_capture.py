"""Bounded execution observations; never a resumable Trial checkpoint."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from dataclasses import replace
from functools import wraps
from typing import Any, Callable, Iterator

from chatcopilot.evals.redaction import collect_env_secrets, redact_payload

_sink: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "evaluation_capture_sink", default=None
)
_current: ContextVar[dict[str, Any] | None] = ContextVar("evaluation_capture", default=None)
_TEXT_LIMIT = 128 * 1024
_BYTE_LIMIT = 512 * 1024


@contextmanager
def capture(sink: Callable[[dict[str, Any]], None] | None = None) -> Iterator[dict[str, Any]]:
    value: dict[str, Any] = {"turns": [], "state": "not_recorded"}
    token = _current.set(value)
    sink_token = _sink.set(sink if sink is not None else _sink.get())
    try:
        yield value
    finally:
        _current.reset(token)
        _sink.reset(sink_token)


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
    sink = _sink.get()
    if sink is not None:
        sink(current)


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
                metadata={**metadata, "execution": observed},
            )

    return wrapped
