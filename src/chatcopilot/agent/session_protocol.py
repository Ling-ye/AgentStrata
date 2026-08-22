"""Shared protocol for pluggable main-agent session implementations."""
from __future__ import annotations

from typing import Any, Protocol

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.prompt import PromptPlan


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

    @property
    def prompt_prefix_length(self) -> int:
        """Host-recorded renderer prefix length for provenance-aware views."""

    def run_task(self, task: AgentTask, *, on_event: EventSink) -> AgentResult:
        """Run one user task and return the final structured result."""

    def set_prompt_plan(self, plan: PromptPlan) -> None:
        """Replace the session prompt plan."""

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """Record a deterministic exchange that did not enter the agent loop."""

    def snapshot_messages(self) -> list[dict[str, Any]]:
        """Return a serializable snapshot of the session messages."""


__all__ = ["AgentSessionProtocol"]
