"""Session adapter shared by all main-agent backends."""
from __future__ import annotations

from typing import Any

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.agent_backend import (
    AgentBackend,
    BackendCapabilities,
    BackendSessionRef,
)
from chatcopilot.contracts.cancellation import CancellationProbe, CancellationRequested
from chatcopilot.contracts.prompt import PromptPlan


class BackendAgentSession:
    """Session surface backed by one opaque backend reference."""

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

    def run_task(
        self,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        try:
            if cancellation is None:
                result = self.backend.stream_turn(
                    self._session_ref,
                    task,
                    on_event=on_event,
                )
            else:
                result = self.backend.stream_turn(
                    self._session_ref,
                    task,
                    on_event=on_event,
                    cancellation=cancellation,
                )
        except CancellationRequested:
            return AgentResult(
                final_text="",
                stop_reason="cancelled",
                message_count=self.message_count,
            )
        self.backend_session_ref
        return result

    def close(self) -> None:
        self.backend.close_session(self._session_ref)

    def discard(self) -> None:
        """Close a session after an external consistency failure and drop resume state."""

        discard_session = getattr(self.backend, "discard_session", None)
        if callable(discard_session):
            discard_session(self._session_ref)
            return
        self.close()

    def set_prompt_plan(self, plan: PromptPlan) -> None:
        getattr(self.backend, "set_prompt_plan")(self._session_ref, plan)

    @property
    def tool_executor(self) -> Any:
        """Return the executor owned by this concrete session.

        Background jobs use the same projected tool surface as the session but do
        not run a model turn.  This explicit property avoids leaking backend-private
        session objects or relying on attribute forwarding.
        """

        concrete = getattr(self.backend, "native_session")(self._session_ref)
        executor = getattr(concrete, "executor", None)
        if executor is None:
            executor = getattr(concrete, "relay_executor", None)
        if executor is None:
            raise RuntimeError("backend session has no tool executor")
        return executor

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

    @property
    def prompt_prefix_length(self) -> int:
        """Expose the concrete in-process prefix; opaque backends own their framing."""

        native_session = getattr(self.backend, "native_session", None)
        if not callable(native_session):
            return 0
        concrete = native_session(self._session_ref)
        value = getattr(concrete, "prompt_prefix_length", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("backend session has invalid prompt prefix provenance")
        return value

__all__ = ["BackendAgentSession"]
