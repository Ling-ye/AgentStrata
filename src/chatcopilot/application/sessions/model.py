"""Transport-neutral session state owned by the application runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.model_selection import CodeModelSelection
from chatcopilot.contracts.persona_control import PendingPersonaProposal
from chatcopilot.contracts.workspace import WorkspaceView


class DiscardableExecutionSession(Protocol):
    """Execution handle that can discard backend resume state during eviction."""

    def discard(self) -> None: ...


class CloseableExecutionSession(Protocol):
    """Execution handle with a normal close operation."""

    def close(self) -> None: ...


ExecutionSession: TypeAlias = DiscardableExecutionSession | CloseableExecutionSession


def _require_text(value: str, *, label: str, max_chars: int = 256) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(value) > max_chars or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains invalid characters")


def _require_generation(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("writer_generation must be a positive integer")


@dataclass(frozen=True)
class GatewaySessionState:
    """Conversation control state without workspace, role, or Agent side effects."""

    session_id: str
    account: ChannelAccountRef
    conversation: ConversationRef
    writer_generation: int
    mode: str = "default"
    debug: bool = False
    event_cursor: int = 0
    active_run_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.session_id, label="session_id")
        _require_text(self.account.channel, label="account channel", max_chars=64)
        _require_text(self.account.account_id, label="account_id")
        _require_text(self.conversation.kind, label="conversation kind", max_chars=64)
        _require_text(
            self.conversation.conversation_id,
            label="conversation_id",
        )
        _require_generation(self.writer_generation)
        _require_text(self.mode, label="mode", max_chars=64)
        if type(self.debug) is not bool:
            raise ValueError("debug must be a boolean")
        if type(self.event_cursor) is not int or self.event_cursor < 0:
            raise ValueError("event_cursor must be a non-negative integer")
        if self.active_run_id is not None:
            _require_text(self.active_run_id, label="active_run_id")


@dataclass(frozen=True)
class ActorSessionKey:
    """Execution-state key bound to one Gateway session and one authorized actor."""

    gateway_session_id: str
    actor_ref: str

    def __post_init__(self) -> None:
        _require_text(self.gateway_session_id, label="gateway_session_id")
        _require_text(self.actor_ref, label="actor_ref")


@dataclass(frozen=True)
class ActorExecutionState:
    """Actor-bound execution state materialized only after authorization."""

    key: ActorSessionKey
    principal: Principal
    writer_generation: int
    workspace: WorkspaceView | None = field(default=None, repr=False)
    agent_session: ExecutionSession | None = field(default=None, repr=False)
    model_selection: CodeModelSelection | None = None
    one_shot_model_selection: CodeModelSelection | None = None
    journal_cursor: int = 0
    persona_proposal: PendingPersonaProposal | None = field(default=None, repr=False)
    turn_context: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        _require_generation(self.writer_generation)
        if self.principal.actor_ref != self.key.actor_ref:
            raise ValueError("ActorSessionKey does not match Principal.actor_ref")
        if type(self.journal_cursor) is not int or self.journal_cursor < 0:
            raise ValueError("journal_cursor must be a non-negative integer")
        if not isinstance(self.turn_context, str):
            raise ValueError("turn_context must be a string")


__all__ = [
    "ActorExecutionState",
    "ActorSessionKey",
    "CloseableExecutionSession",
    "DiscardableExecutionSession",
    "ExecutionSession",
    "GatewaySessionState",
]
