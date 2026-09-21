"""Session adapter shared by all main-agent runtimes."""
from __future__ import annotations

from dataclasses import replace

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.runtime_adapter import (
    RuntimeAdapter,
    RuntimeCapabilities,
    RuntimeSessionRef,
)
from chatcopilot.contracts.cancellation import CancellationProbe, CancellationRequested
from chatcopilot.contracts.cancellation import CancellationToken, CombinedCancellation
from chatcopilot.contracts.prompt import PromptPlan
from chatcopilot.contracts.execution import TranscriptSnapshot, CapabilitySnapshot


class RuntimeAgentSession:
    """Session surface backed by one opaque runtime adapter reference."""

    def __init__(
        self,
        adapter: RuntimeAdapter,
        session_ref: RuntimeSessionRef,
        *,
        allowed_tool_names: frozenset[str] = frozenset(),
        capability_snapshot: CapabilitySnapshot = CapabilitySnapshot(),
        policy_revision: str = "",
    ) -> None:
        self._adapter = adapter
        self._session_ref = session_ref
        self._capabilities = adapter.capabilities.intersect_tools(allowed_tool_names)
        self.capability_snapshot = capability_snapshot
        self.policy_revision = policy_revision
        self._active_cancellation = None

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    @property
    def runtime_session_ref(self) -> RuntimeSessionRef:
        current = getattr(self._adapter, "current_session_ref", None)
        if callable(current):
            self._session_ref = current(self._session_ref)
        return self._session_ref

    @property
    def runtime_id(self) -> str:
        return self.runtime_session_ref.runtime_id

    def run_task(
        self,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        self._active_cancellation = CancellationToken()
        cancellation = CombinedCancellation(cancellation, self._active_cancellation)
        try:
            if cancellation is None:
                result = self._adapter.stream_turn(
                    self._session_ref,
                    task,
                    on_event=on_event,
                )
            else:
                result = self._adapter.stream_turn(
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
        finally:
            self._active_cancellation = None
        self.runtime_session_ref
        return result

    def close(self) -> None:
        self._adapter.close_session(self._session_ref)

    def discard(self) -> None:
        """Close a session after an external consistency failure and drop resume state."""

        self._adapter.discard_session(self._session_ref)

    def set_prompt_plan(self, plan: PromptPlan) -> None:
        self.update_context(plan)

    def update_context(self, plan: PromptPlan) -> None:
        self._adapter.set_prompt_plan(self._session_ref, replace(plan, capability_digest=self.capability_snapshot.fingerprint,
                                                             policy_revision=self.policy_revision))

    def snapshot_transcript(self) -> TranscriptSnapshot:
        return self._adapter.snapshot_transcript(self._session_ref)

    def cancel(self) -> None:
        if self._active_cancellation is not None:
            self._active_cancellation.cancel()

    @property
    def busy(self) -> bool:
        return self._active_cancellation is not None or self._adapter.is_busy(self._session_ref)

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        self._adapter.record_exchange(
            self._session_ref, user_text, assistant_text
        )

    @property
    def message_count(self) -> int:
        return len(self.snapshot_transcript().messages)

__all__ = ["RuntimeAgentSession"]
