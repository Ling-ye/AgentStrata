"""Prepared application turns and process-local exchange references."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from chatcopilot.contracts.agent import AgentResult, ResourceRef
from chatcopilot.contracts.authorization import Principal


@dataclass(frozen=True)
class PreparedTurn:
    run_id: str
    session_id: str
    principal: Principal
    canonical_text: str
    resource_refs: tuple[ResourceRef, ...] = ()
    turn_context: str = ""
    message_id: str | None = None
    sender_display_name: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, eq=False)
class ExchangeRef:
    """Identity-only handle valid in the application process that created it."""


@dataclass(frozen=True)
class TurnOutcome:
    result: AgentResult
    exchange: ExchangeRef | None = None
