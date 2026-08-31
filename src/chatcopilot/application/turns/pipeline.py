"""Fixed fail-closed turn pipeline independent from ACP and Channel protocols."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from chatcopilot.application.turns.model import (
    StageResult,
    TURN_STAGE_ORDER,
    TurnContext,
    TurnDirective,
    TurnStage,
)


class TurnHandler(Protocol):
    stage: TurnStage

    async def handle(self, context: TurnContext) -> StageResult: ...


@dataclass(frozen=True)
class CallbackTurnHandler:
    stage: TurnStage
    callback: Callable[[TurnContext], Awaitable[StageResult]]

    async def handle(self, context: TurnContext) -> StageResult:
        return await self.callback(context)


class OrderedTurnPipeline:
    """Run every authoritative stage once, skipping side effects after terminal decisions."""

    def __init__(self, handlers: Sequence[TurnHandler]) -> None:
        stages = tuple(handler.stage for handler in handlers)
        if stages != TURN_STAGE_ORDER:
            raise ValueError(
                "turn handlers must match the fixed stage order: "
                + ", ".join(stage.value for stage in TURN_STAGE_ORDER)
            )
        self._handlers = tuple(handlers)

    async def run(self, context: TurnContext) -> StageResult:
        jump_to: TurnStage | None = None
        final = StageResult()
        for handler in self._handlers:
            if jump_to is not None and handler.stage is not jump_to:
                context.skip(handler.stage)
                continue

            result = await handler.handle(context)
            context.complete(handler.stage)
            final = result
            if handler.stage is TurnStage.FINISH:
                break
            if result.directive is TurnDirective.OUTBOUND:
                jump_to = TurnStage.OUTBOUND_PERSISTED
            elif result.directive is TurnDirective.FINISH:
                jump_to = TurnStage.FINISH
            elif jump_to is handler.stage:
                jump_to = None

        if context.accounted_stages != TURN_STAGE_ORDER:
            raise RuntimeError("turn pipeline did not account for every authoritative stage")
        return final


__all__ = ["CallbackTurnHandler", "OrderedTurnPipeline", "TurnHandler"]
