from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from chatcopilot.application.turns import (
    CallbackTurnHandler,
    OrderedTurnPipeline,
    StageResult,
    TURN_STAGE_ORDER,
    TurnContext,
    TurnDirective,
    TurnStage,
)
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    SenderClaim,
    TransportEvidence,
)


def _context() -> TurnContext:
    account = ChannelAccountRef(channel="qq", account_id="12345")
    conversation = ConversationRef(kind="group", conversation_id="67890")
    event = CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=account,
            conversation=conversation,
            sender=SenderClaim(sender_id="54321"),
            event_id="event-1",
            message_id="message-1",
            connection_generation="generation-1",
            frame_sha256="a" * 64,
            observed_at=1.0,
        ),
        segments=(MessageSegment(kind="text", text="hello"),),
    )
    return TurnContext(run_id="run-1", session_id="session-1", inbound=event)


def _handlers(
    callback_for: Callable[
        [TurnStage],
        Callable[[TurnContext], Awaitable[StageResult]],
    ],
) -> tuple[CallbackTurnHandler, ...]:
    return tuple(
        CallbackTurnHandler(stage=stage, callback=callback_for(stage))
        for stage in TURN_STAGE_ORDER
    )


def test_pipeline_runs_the_fixed_authority_order() -> None:
    observed: list[TurnStage] = []

    def callback_for(stage: TurnStage):
        async def callback(_context: TurnContext) -> StageResult:
            observed.append(stage)
            return StageResult()

        return callback

    context = _context()
    asyncio.run(OrderedTurnPipeline(_handlers(callback_for)).run(context))

    assert tuple(observed) == TURN_STAGE_ORDER
    assert tuple(context.completed_stages) == TURN_STAGE_ORDER
    assert context.skipped_stages == []


def test_admission_denial_skips_agent_side_effects_but_persists_outbound_and_finishes() -> None:
    observed: list[TurnStage] = []

    def callback_for(stage: TurnStage):
        async def callback(_context: TurnContext) -> StageResult:
            observed.append(stage)
            if stage is TurnStage.ADMISSION:
                return StageResult(TurnDirective.OUTBOUND, "admission-denied")
            return StageResult()

        return callback

    context = _context()
    asyncio.run(OrderedTurnPipeline(_handlers(callback_for)).run(context))

    assert observed == [
        TurnStage.TRANSPORT_VERIFIED,
        TurnStage.INTAKE_RECORDED,
        TurnStage.ADMISSION,
        TurnStage.OUTBOUND_PERSISTED,
        TurnStage.DELIVERY_OBSERVED,
        TurnStage.FINISH,
    ]
    assert TurnStage.RESOURCE_MATERIALIZATION in context.skipped_stages
    assert TurnStage.EXECUTION in context.skipped_stages
    assert context.accounted_stages == TURN_STAGE_ORDER


def test_tracking_failure_skips_all_later_side_effects_except_finish() -> None:
    observed: list[TurnStage] = []

    def callback_for(stage: TurnStage):
        async def callback(_context: TurnContext) -> StageResult:
            observed.append(stage)
            if stage is TurnStage.INTAKE_RECORDED:
                return StageResult(TurnDirective.FINISH, "task-persistence-failed")
            return StageResult()

        return callback

    context = _context()
    asyncio.run(OrderedTurnPipeline(_handlers(callback_for)).run(context))

    assert observed == [
        TurnStage.TRANSPORT_VERIFIED,
        TurnStage.INTAKE_RECORDED,
        TurnStage.FINISH,
    ]
    assert TurnStage.ADMISSION in context.skipped_stages
    assert TurnStage.EXECUTION in context.skipped_stages
    assert TurnStage.OUTBOUND_PERSISTED in context.skipped_stages


def test_pipeline_rejects_missing_or_reordered_handlers() -> None:
    async def callback(_context: TurnContext) -> StageResult:
        return StageResult()

    handlers = [CallbackTurnHandler(stage=stage, callback=callback) for stage in TURN_STAGE_ORDER]

    with pytest.raises(ValueError, match="fixed stage order"):
        OrderedTurnPipeline(tuple(reversed(handlers)))
    with pytest.raises(ValueError, match="fixed stage order"):
        OrderedTurnPipeline(handlers[:-1])
