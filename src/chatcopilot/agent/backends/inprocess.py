"""Native and LangGraph backend adapters."""
from __future__ import annotations

import uuid
from typing import Any

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.agent_backend import (
    BackendCapabilities,
    BackendOpenRequest,
    BackendSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_REPOSITORY_MUTATION,
    CAPABILITY_TOOLS,
    require_backend_capabilities,
)


class InProcessAgentBackend:
    def __init__(self, backend_id: str, *, tool_names: set[str]) -> None:
        self.backend_id = backend_id
        self._capabilities = BackendCapabilities(
            names=frozenset(
                {CAPABILITY_CHAT, CAPABILITY_TOOLS, CAPABILITY_REPOSITORY_MUTATION}
            ),
            tool_names=frozenset(tool_names),
        )
        self._sessions: dict[str, Any] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def open_session(self, request: BackendOpenRequest) -> BackendSessionRef:
        require_backend_capabilities(
            self.backend_id, self.capabilities, request.required_capabilities
        )
        factory = request.options.get("session_factory")
        if not callable(factory):
            raise TypeError("in-process backend requires a session_factory")
        value = f"{self.backend_id}:{uuid.uuid4().hex}"
        self._sessions[value] = factory()
        return BackendSessionRef(self.backend_id, value)

    def stream_turn(
        self,
        session: BackendSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
    ) -> AgentResult:
        return self.native_session(session).run_task(task, on_event=on_event)

    def close_session(self, session: BackendSessionRef) -> None:
        concrete = self._sessions.pop(session.value, None)
        close = getattr(concrete, "close", None)
        if callable(close):
            close()

    def native_session(self, session: BackendSessionRef) -> Any:
        if session.backend != self.backend_id or session.value not in self._sessions:
            raise KeyError("unknown or cross-backend session reference")
        return self._sessions[session.value]

    def current_session_ref(self, session: BackendSessionRef) -> BackendSessionRef:
        self.native_session(session)
        return session

    def set_system_baseline(self, session: BackendSessionRef, baseline: str) -> None:
        self.native_session(session).set_system_baseline(baseline)

    def record_exchange(
        self, session: BackendSessionRef, user_text: str, assistant_text: str
    ) -> None:
        self.native_session(session).record_exchange(user_text, assistant_text)

    def snapshot_messages(self, session: BackendSessionRef) -> list[dict[str, Any]]:
        return self.native_session(session).snapshot_messages()


__all__ = ["InProcessAgentBackend"]
