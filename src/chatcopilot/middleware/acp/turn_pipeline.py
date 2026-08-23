"""Typed, ordered ACP turn pipeline primitives."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from acp import PromptResponse

TURN_STAGE_ORDER = (
    "attachments",
    "permissions",
    "deterministic_shortcuts",
    "session_materialization",
    "execution",
    "finish",
)


@dataclass
class TurnContext:
    session_id: str
    session: Any
    user_text: str
    message_id: str | None
    turn_task: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    completed_stages: list[str] = field(default_factory=list)

    def complete(self, stage: str) -> None:
        expected = TURN_STAGE_ORDER[len(self.completed_stages)]
        if stage != expected:
            raise RuntimeError(
                f"ACP turn stage out of order: expected {expected!r}, got {stage!r}"
            )
        self.completed_stages.append(stage)


@dataclass(frozen=True)
class TurnOutcome:
    response: PromptResponse | None = None
    stop: bool = False
    reason: str = ""


class TurnHandler(Protocol):
    name: str

    async def handle(self, context: TurnContext) -> TurnOutcome: ...


class OrderedTurnPipeline:
    def __init__(self, handlers: Sequence[TurnHandler]) -> None:
        names = tuple(handler.name for handler in handlers)
        if names != TURN_STAGE_ORDER:
            raise ValueError(
                "ACP turn handlers must match the fixed stage order: "
                + ", ".join(TURN_STAGE_ORDER)
            )
        self._handlers = tuple(handlers)

    async def run(self, context: TurnContext) -> TurnOutcome:
        outcome = TurnOutcome()
        for handler in self._handlers:
            outcome = await handler.handle(context)
            context.complete(handler.name)
            if outcome.stop:
                return outcome
        return outcome


@dataclass(frozen=True)
class CallbackTurnHandler:
    name: str
    callback: Callable[[TurnContext], Awaitable[TurnOutcome]]

    async def handle(self, context: TurnContext) -> TurnOutcome:
        return await self.callback(context)


__all__ = [
    "CallbackTurnHandler",
    "OrderedTurnPipeline",
    "TURN_STAGE_ORDER",
    "TurnContext",
    "TurnHandler",
    "TurnOutcome",
]
