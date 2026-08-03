"""Session adapter shared by all main-agent backends."""
from __future__ import annotations

from typing import Any

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.agent_backend import (
    AgentBackend,
    BackendCapabilities,
    BackendSessionRef,
)


class BackendAgentSession:
    """Compatibility session surface backed by an opaque backend reference."""

    def __init__(
        self,
        backend: AgentBackend,
        session_ref: BackendSessionRef,
        *,
        allowed_tool_names: frozenset[str] = frozenset(),
    ) -> None:
        self.backend = backend
        self._session_ref = session_ref
        self._capabilities = backend.capabilities.intersect_tools(allowed_tool_names)

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    @property
    def backend_session_ref(self) -> BackendSessionRef:
        current = getattr(self.backend, "current_session_ref", None)
        if callable(current):
            self._session_ref = current(self._session_ref)
        return self._session_ref

    @property
    def backend_id(self) -> str:
        return self.backend_session_ref.backend

    def run_task(self, task: AgentTask, *, on_event: EventSink) -> AgentResult:
        result = self.backend.stream_turn(self._session_ref, task, on_event=on_event)
        self.backend_session_ref
        return result

    def close(self) -> None:
        self.backend.close_session(self._session_ref)

    def set_system_baseline(self, baseline: str) -> None:
        getattr(self.backend, "set_system_baseline")(self._session_ref, baseline)

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        getattr(self.backend, "record_exchange")(
            self._session_ref, user_text, assistant_text
        )

    def snapshot_messages(self) -> list[dict[str, Any]]:
        return getattr(self.backend, "snapshot_messages")(self._session_ref)

    @property
    def message_count(self) -> int:
        return len(self.snapshot_messages())

    @property
    def _messages(self) -> list[dict[str, Any]]:
        return self.snapshot_messages()

    def __getattr__(self, name: str) -> Any:
        """Keep concrete-session diagnostics compatible during the migration."""

        resolver = getattr(self.backend, "native_session", None)
        if callable(resolver):
            return getattr(resolver(self._session_ref), name)
        raise AttributeError(name)


__all__ = ["BackendAgentSession"]
