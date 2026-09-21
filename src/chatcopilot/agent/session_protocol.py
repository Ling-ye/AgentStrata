"""Shared protocol for pluggable main-agent session implementations."""
from __future__ import annotations

from typing import Protocol

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.cancellation import CancellationProbe
from chatcopilot.contracts.prompt import PromptPlan
from chatcopilot.contracts.execution import TranscriptSnapshot
from chatcopilot.contracts.model_runtime import RuntimeId


class AgentSessionProtocol(Protocol):
    """Minimal session surface consumed outside the concrete agent loop.

    Native ``AgentSession`` and the LangGraph-backed session both implement this
    interface so middleware can stay independent from the selected agent runtime.
    """

    @property
    def runtime_id(self) -> RuntimeId:
        """Immutable execution Runtime identity bound by the resolved route."""

    @property
    def message_count(self) -> int:
        """Number of messages currently tracked by the session."""

    def run_task(
        self,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        """Run one user task and return the final structured result."""

    def update_context(self, plan: PromptPlan) -> None:
        """Replace the session prompt plan."""

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """Record a deterministic exchange that did not enter the agent loop."""

    def snapshot_transcript(self) -> TranscriptSnapshot:
        """Return a host-visible transcript with explicit coverage."""

    def close(self) -> None: ...

    def discard(self) -> None: ...

    def cancel(self) -> None: ...


__all__ = ["AgentSessionProtocol"]
