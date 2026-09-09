"""Bounded live message snapshots shared by the in-process backends."""
from __future__ import annotations

import time

from chatcopilot.contracts.agent import AgentMessageObserved, EventSink
from chatcopilot.agent.turn_support import safe_emit


class ProcessMessage:
    def __init__(self, sink: EventSink, *, trace_id: str, parent_span_id: str,
                 backend: str, depth: int) -> None:
        self.sink = sink
        self.trace_id = trace_id
        self.parent = parent_span_id
        self.backend = backend
        self.depth = depth
        self.text = ""
        self.truncated = False
        self.revision = 0
        self.last_emit = 0.0

    def append(self, text: str) -> None:
        self._replace(self.text + text)
        if self.revision < 32 and time.monotonic() - self.last_emit >= 1:
            self._emit("update", "running")

    def finish(self, text: str | None = None, *, status: str = "succeeded") -> None:
        if text is not None:
            self._replace(text)
        if self.text:
            self._emit("finish", status)

    def _replace(self, text: str) -> None:
        raw = text.encode("utf-8", errors="replace")
        self.truncated = self.truncated or len(raw) > 48 * 1024
        self.text = raw[:48 * 1024].decode("utf-8", errors="ignore")

    def _emit(self, phase: str, status: str) -> None:
        self.revision += 1
        self.last_emit = time.monotonic()
        safe_emit(self.sink, AgentMessageObserved(
            text=self.text, message_id=f"message:{self.parent}",
            trace_id=self.trace_id, span_id=f"message:{self.parent}", parent_span_id=self.parent,
            revision=self.revision, phase="finish" if phase == "finish" else "update", status=status,
            backend=self.backend, depth=self.depth + 1, observed_at=time.time(),
            capture_state="truncated" if self.truncated else "available",
        ))
