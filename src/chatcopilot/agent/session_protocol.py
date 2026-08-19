"""Shared protocol for pluggable main-agent session implementations."""
from __future__ import annotations

from typing import Any, Protocol

from chatcopilot.agent.protocol import AgentResult, AgentTask, EventSink


class AgentSessionProtocol(Protocol):
    """Minimal session surface consumed outside the concrete agent loop.

    Native ``AgentSession`` and the LangGraph-backed session both implement this
    interface so middleware can stay independent from the selected agent backend.
    """

    @property
    def message_count(self) -> int:
        """Number of messages currently tracked by the session."""

    @property
    def _messages(self) -> list[dict[str, Any]]:
        """Raw message history snapshot used by debug/transcript code."""

    def run_task(self, task: AgentTask, *, on_event: EventSink) -> AgentResult:
        """Run one user task and return the final structured result."""

    def set_system_baseline(self, baseline: str) -> None:
        """Replace the session system baseline."""

    def set_system_context(
        self,
        baseline: str,
        *,
        session_dynamic_tail: str | None = None,
        memory_snippet: str | None = None,
    ) -> None:
        """Replace the baseline plus current persona and memory snapshots."""

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """Record a deterministic exchange that did not enter the agent loop."""

    def snapshot_messages(self) -> list[dict[str, Any]]:
        """Return a serializable snapshot of the session messages."""


__all__ = ["AgentSessionProtocol"]
