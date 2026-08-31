from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

import pytest

from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError,
    ChannelDeliveryUnknownError,
    ChannelHealth,
)
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    DeliveryReceipt,
    MessageSegment,
    OutboundEnvelope,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.gateway_protocol import EventFrame
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.gateway.channels import ChannelRuntimeError, ChannelRuntimeManager
from chatcopilot.gateway.state_store import (
    GatewayStateStore,
    IngressConflict,
    OutboundConflict,
    StaleWriterGeneration,
)


ACCOUNT = ChannelAccountRef(channel="qq", account_id="10001")
OTHER_ACCOUNT = ChannelAccountRef(channel="qq", account_id="10002")
CONVERSATION = ConversationRef(kind="group", conversation_id="20001")


def _inbound(
    *,
    event_id: str = "event-1",
    body: str = "hello",
    connection_generation: str = "connection-main",
    account: ChannelAccountRef = ACCOUNT,
) -> CanonicalInboundEvent:
    return CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=account,
            conversation=CONVERSATION,
            sender=SenderClaim(sender_id="30001", display_name="sender"),
            event_id=event_id,
            message_id="message-1",
            connection_generation=connection_generation,
            frame_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            observed_at=100.0,
        ),
        segments=(MessageSegment(kind="text", text=body),),
    )


def _outbound(
    *,
    outbound_id: str = "outbound-1",
    body: str = "reply",
    account: ChannelAccountRef = ACCOUNT,
) -> OutboundEnvelope:
    return OutboundEnvelope(
        outbound_id=outbound_id,
        account=account,
        conversation=CONVERSATION,
        segments=(MessageSegment(kind="text", text=body),),
        created_at=200.0,
        session_id="session-1",
        run_id="run-1",
    )


def _principal(event: CanonicalInboundEvent) -> Principal:
    evidence = event.evidence
    return Principal(
        channel=evidence.account.channel,
        account_id=evidence.account.account_id,
        conversation=ConversationIdentity(
            platform=evidence.account.channel,
            chat_kind=evidence.conversation.kind,
            chat_id=evidence.conversation.conversation_id,
        ),
        user_id=evidence.sender.sender_id,
        role=Role.USER,
        evidence_digest="sha256:" + evidence.frame_sha256,
    )


class _Ingress:
    def __init__(self) -> None:
        self.events: list[CanonicalInboundEvent] = []
        self.principals: list[Principal] = []
        self.authorization_calls = 0
        self.error: BaseException | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    def authorize_inbound(self, event: CanonicalInboundEvent) -> Principal:
        self.authorization_calls += 1
        evidence = event.evidence
        return Principal(
            channel=evidence.account.channel,
            account_id=evidence.account.account_id,
            conversation=ConversationIdentity(
                platform=evidence.account.channel,
                chat_kind=evidence.conversation.kind,
                chat_id=evidence.conversation.conversation_id,
            ),
            user_id=evidence.sender.sender_id,
            role=Role.USER,
            evidence_digest="sha256:" + evidence.frame_sha256,
        )

    async def handle_authorized_inbound(
        self,
        event: CanonicalInboundEvent,
        principal: Principal,
    ) -> None:
        self.events.append(event)
        self.principals.append(principal)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


class _Sink:
    def __init__(self, *, fail: bool = False) -> None:
        self.frames: list[EventFrame] = []
        self.fail = fail

    def publish(self, frame: EventFrame) -> None:
        self.frames.append(frame)
        if self.fail:
            raise RuntimeError("subscriber secret must stay private")


class _Driver:
    def __init__(
        self,
        channel_id: str,
        account: ChannelAccountRef,
        log: list[str],
        *,
        start_error: BaseException | None = None,
        send_error: BaseException | None = None,
        receipt: DeliveryReceipt | None = None,
    ) -> None:
        self._channel_id = channel_id
        self.account = account
        self.log = log
        self.start_error = start_error
        self.send_error = send_error
        self.receipt = receipt
        self.state = "stopped"
        self.connection_generation: str | None = None
        self.detail_code: str | None = None
        self.send_calls = 0

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def start(self) -> None:
        self.log.append(f"start:{self.channel_id}")
        if self.start_error is not None:
            raise self.start_error
        self.state = "ready"
        self.connection_generation = f"connection-{self.channel_id}"

    async def stop(self) -> None:
        self.log.append(f"stop:{self.channel_id}")
        self.state = "stopped"
        self.connection_generation = None

    def health(self) -> ChannelHealth:
        return ChannelHealth(
            channel_id=self.channel_id,
            account=self.account,
            state=self.state,  # type: ignore[arg-type]
            connection_generation=self.connection_generation,
            detail_code=self.detail_code,
        )

    async def send(self, envelope: OutboundEnvelope) -> DeliveryReceipt:
        self.send_calls += 1
        if self.send_error is not None:
            raise self.send_error
        return self.receipt or DeliveryReceipt(
            receipt_id="driver-receipt",
            outbound_id=envelope.outbound_id,
            stage="provider_acknowledged",
            observed_at=203.0,
            provider_message_id="provider-message-1",
        )


class ChannelRuntimeManagerTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="asg-gateway-channels-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.state_store = GatewayStateStore(Path(temporary.name) / "state")
        self.ingress = _Ingress()
        self.sink = _Sink()
        self.manager = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=self.ingress,
            event_sink=self.sink,
        )
        self.log: list[str] = []
        self.driver = _Driver("main", ACCOUNT, self.log)
        self.manager.register(self.driver)

    async def test_registers_unique_channel_and_account_and_stops_in_reverse(self) -> None:
        with pytest.raises(ChannelRuntimeError) as duplicate_channel:
            self.manager.register(_Driver("main", OTHER_ACCOUNT, self.log))
        assert duplicate_channel.value.code == "channel_id_duplicate"

        with pytest.raises(ChannelRuntimeError) as duplicate_account:
            self.manager.register(_Driver("other", ACCOUNT, self.log))
        assert duplicate_account.value.code == "channel_account_duplicate"

        second = _Driver("second", OTHER_ACCOUNT, self.log)
        self.manager.register(second)
        await self.manager.start()
        await self.manager.activate()
        await self.manager.stop()

        assert self.log == ["start:main", "start:second", "stop:second", "stop:main"]
        assert self.manager.health().state == "stopped"

    async def test_partial_start_rolls_back_started_drivers_without_secret_health(self) -> None:
        manager = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=self.ingress,
        )
        first = _Driver("first", ACCOUNT, self.log)
        failing = _Driver(
            "failing",
            OTHER_ACCOUNT,
            self.log,
            start_error=RuntimeError("provider token must never enter health"),
        )
        manager.register(first)
        manager.register(failing)

        with pytest.raises(ChannelRuntimeError) as caught:
            await manager.start()

        assert caught.value.code == "channel_start_failed"
        assert self.log == ["start:first", "start:failing", "stop:first"]
        snapshot = manager.health()
        assert snapshot.state == "error"
        assert snapshot.detail_code == "channel_start_failed"
        assert "provider token" not in repr(snapshot)

    async def test_uses_preacquired_generation_without_fencing_the_gateway_host(self) -> None:
        generation = self.state_store.acquire_writer_generation(now=100.0)
        manager = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=self.ingress,
            writer_generation=generation,
        )
        manager.register(_Driver("preacquired", OTHER_ACCOUNT, self.log))

        await manager.start()

        assert self.state_store.current_writer_generation() == generation
        assert manager.health().writer_generation == generation
        assert manager.health().state == "prepared"

        await manager.activate()

        assert manager.health().state == "ready"

    async def test_rejects_stale_preacquired_generation_before_starting_driver(self) -> None:
        generation = self.state_store.acquire_writer_generation(now=100.0)
        self.state_store.acquire_writer_generation(now=101.0)
        manager = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=self.ingress,
            writer_generation=generation,
        )
        manager.register(_Driver("stale", OTHER_ACCOUNT, self.log))

        with pytest.raises(ChannelRuntimeError) as caught:
            await manager.start()

        assert caught.value.code == "channel_writer_generation_stale"
        assert self.log == []
        assert manager.health().state == "error"

    async def test_prepared_runtime_fences_inbound_and_outbound_until_activation(
        self,
    ) -> None:
        await self.manager.start()
        event = _inbound()
        inbound = asyncio.create_task(self.manager.handle_inbound(event))
        await asyncio.sleep(0)

        assert not inbound.done()
        assert self.ingress.authorization_calls == 0
        assert self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-1",
        ) is None
        with pytest.raises(ChannelRuntimeError) as outbound:
            await self.manager.send(_outbound())
        assert outbound.value.code == "channel_runtime_not_ready"
        assert self.state_store.get_outbound("outbound-1") is None
        assert self.driver.send_calls == 0

        await self.manager.activate()
        await inbound

        assert self.ingress.authorization_calls == 1
        assert self.ingress.events == [event]

    async def test_stop_before_activation_wakes_inbound_without_side_effects(self) -> None:
        await self.manager.start()
        inbound = asyncio.create_task(self.manager.handle_inbound(_inbound()))
        await asyncio.sleep(0)

        await self.manager.stop()

        with pytest.raises(ChannelRuntimeError) as caught:
            await inbound
        assert caught.value.code == "channel_runtime_not_ready"
        assert self.ingress.authorization_calls == 0
        assert self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-1",
        ) is None

    async def test_inbound_waiting_for_claim_lock_rechecks_runtime_after_stop(
        self,
    ) -> None:
        await self.manager.start()
        await self.manager.activate()
        await self.manager._ingress_claim_lock.acquire()
        inbound = asyncio.create_task(self.manager.handle_inbound(_inbound()))
        await asyncio.sleep(0)

        await self.manager.stop()
        self.manager._ingress_claim_lock.release()

        with pytest.raises(ChannelRuntimeError) as caught:
            await inbound
        assert caught.value.code == "channel_runtime_not_ready"
        assert self.ingress.authorization_calls == 0
        assert self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-1",
        ) is None

    async def test_new_ingress_is_durable_before_application_and_duplicate_is_not_replayed(
        self,
    ) -> None:
        await self.manager.start()
        await self.manager.activate()
        event = _inbound()

        await self.manager.handle_inbound(event)
        record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-1",
        )
        assert record is not None
        assert record.state == "completed"
        assert self.ingress.events == [event]

        await self.manager.handle_inbound(event)
        assert self.ingress.events == [event]
        assert self.ingress.authorization_calls == 1

    async def test_runtime_bounds_terminal_ingress_without_deleting_active(self) -> None:
        manager = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=self.ingress,
            ingress_retention_limit=2,
        )
        manager.register(_Driver("bounded", ACCOUNT, self.log))
        await manager.start()
        await manager.activate()
        for index in range(5):
            await manager.handle_inbound(
                _inbound(
                    event_id=f"bounded-{index}",
                    body=f"bounded-body-{index}",
                    connection_generation="connection-bounded",
                )
            )
        generation = manager.health().writer_generation
        assert generation is not None
        active = _inbound(
            event_id="bounded-active",
            body="bounded-active",
            connection_generation="connection-bounded",
        )
        self.state_store.reserve_ingress(
            generation=generation,
            event=active,
            principal=_principal(active),
        )

        terminal = self.state_store.list_ingress(
            states=("completed", "failed"),
            limit=1000,
        )
        assert len(terminal) == 2
        active_record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="bounded-active",
        )
        assert active_record is not None
        assert active_record.state == "accepted"

    async def test_only_never_claimed_ingress_is_recovered_automatically(
        self,
    ) -> None:
        await self.manager.start()
        await self.manager.activate()
        self.ingress.entered = asyncio.Event()
        self.ingress.release = asyncio.Event()
        event = _inbound()
        first = asyncio.create_task(self.manager.handle_inbound(event))
        await self.ingress.entered.wait()

        processing = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-1",
        )
        assert processing is not None
        assert processing.state == "processing"

        await self.manager.handle_inbound(event)
        assert len(self.ingress.events) == 1
        assert self.ingress.authorization_calls == 1
        self.ingress.release.set()
        await first

        failed_event = _inbound(event_id="event-failed", body="failed")
        self.ingress.error = RuntimeError("application failed")
        with pytest.raises(RuntimeError, match="application failed"):
            await self.manager.handle_inbound(failed_event)
        failed_record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-failed",
        )
        assert failed_record is not None
        assert failed_record.state == "failed"
        await self.manager.handle_inbound(failed_event)
        assert [item.evidence.event_id for item in self.ingress.events].count(
            "event-failed"
        ) == 1

        generation = self.manager.health().writer_generation
        assert generation is not None
        recovery_event = _inbound(event_id="event-recovery", body="recovery")
        self.state_store.reserve_ingress(
            generation=generation,
            event=recovery_event,
            principal=_principal(recovery_event),
        )
        assert self.state_store.claim_ingress(
            generation=generation,
            channel="qq",
            account_id="10001",
            event_id="event-recovery",
        )
        self.state_store.acquire_writer_generation(now=500.0)
        await self.manager.stop()

        replacement_ingress = _Ingress()
        replacement = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=replacement_ingress,
        )
        replacement_driver = _Driver("replacement", ACCOUNT, self.log)
        replacement.register(replacement_driver)
        await replacement.start()
        await replacement.activate()
        await replacement.handle_inbound(
            _inbound(
                event_id="event-recovery",
                body="recovery",
                connection_generation="connection-replacement",
            )
        )
        recovered_record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-recovery",
        )
        assert recovered_record is not None
        assert recovered_record.state == "recovery_required"

        accepted_event = _inbound(
            event_id="event-accepted",
            body="accepted",
            connection_generation="connection-replacement",
        )
        replacement_generation = replacement.health().writer_generation
        assert replacement_generation is not None
        self.state_store.reserve_ingress(
            generation=replacement_generation,
            event=accepted_event,
            principal=_principal(accepted_event),
        )
        await replacement.handle_inbound(accepted_event)
        accepted_record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-accepted",
        )
        assert accepted_record is not None
        assert accepted_record.state == "completed"
        assert replacement_ingress.events[-1] == accepted_event

    async def test_activation_replays_accepted_ingress_once_with_stored_principal(
        self,
    ) -> None:
        await self.manager.start()
        await self.manager.activate()
        generation = self.manager.health().writer_generation
        assert generation is not None
        event = _inbound(event_id="accepted-before-restart", body="recover me")
        admitted = _principal(event)
        admitted = Principal(
            channel=admitted.channel,
            account_id=admitted.account_id,
            conversation=admitted.conversation,
            user_id=admitted.user_id,
            role=Role.OWNER,
            evidence_digest=admitted.evidence_digest,
        )
        self.state_store.reserve_ingress(
            generation=generation,
            event=event,
            principal=admitted,
        )
        await self.manager.stop()

        recovered = _Ingress()
        replacement = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=recovered,
        )
        replacement.register(_Driver("recovery", ACCOUNT, self.log))
        await replacement.start()
        assert recovered.events == []
        await replacement.activate()
        await replacement.activate()

        record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="accepted-before-restart",
        )
        assert record is not None and record.state == "completed"
        assert recovered.events == [event]
        assert recovered.principals == [admitted]
        assert recovered.authorization_calls == 0

    async def test_recovery_generation_change_fences_worker_without_replay(
        self,
    ) -> None:
        await self.manager.start()
        await self.manager.activate()
        generation = self.manager.health().writer_generation
        assert generation is not None
        event = _inbound(event_id="accepted-fenced", body="recover me")
        self.state_store.reserve_ingress(
            generation=generation,
            event=event,
            principal=_principal(event),
        )
        await self.manager.stop()

        replacement_ingress = _Ingress()

        async def fence_after_claim(
            recovered_event: CanonicalInboundEvent,
            principal: Principal,
        ) -> None:
            replacement_ingress.events.append(recovered_event)
            replacement_ingress.principals.append(principal)
            self.state_store.acquire_writer_generation(now=901.0)

        replacement_ingress.handle_authorized_inbound = fence_after_claim  # type: ignore[method-assign]
        replacement = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=replacement_ingress,
        )
        replacement.register(_Driver("recovery-fenced", ACCOUNT, self.log))
        await replacement.start()
        with pytest.raises(ChannelRuntimeError) as caught:
            await replacement.activate()
        assert caught.value.code == "channel_activation_failed"
        record = self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="accepted-fenced",
        )
        assert record is not None and record.state == "recovery_required"
        assert replacement_ingress.events == [event]

        final_ingress = _Ingress()
        final = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=final_ingress,
        )
        final.register(_Driver("recovery-final", ACCOUNT, self.log))
        await final.start()
        await final.activate()
        assert final_ingress.events == []

    async def test_ingress_drift_and_stale_generations_fail_before_application(self) -> None:
        await self.manager.start()
        await self.manager.activate()
        await self.manager.handle_inbound(_inbound())

        with pytest.raises(IngressConflict):
            await self.manager.handle_inbound(_inbound(body="drifted"))
        assert len(self.ingress.events) == 1

        self.state_store.acquire_writer_generation(now=501.0)
        with pytest.raises(StaleWriterGeneration):
            await self.manager.handle_inbound(_inbound(event_id="stale"))
        assert len(self.ingress.events) == 1

    async def test_stale_connection_event_is_rejected_before_persistence(self) -> None:
        await self.manager.start()
        await self.manager.activate()
        with pytest.raises(ChannelRuntimeError) as caught:
            await self.manager.handle_inbound(
                _inbound(event_id="stale-connection", connection_generation="old")
            )
        assert caught.value.code == "channel_connection_generation_mismatch"
        assert self.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="stale-connection",
        ) is None

    async def test_provider_ack_persists_and_publishes_every_delivery_transition(self) -> None:
        await self.manager.start()
        await self.manager.activate()
        receipt = await self.manager.send(_outbound())

        assert receipt.stage == "provider_acknowledged"
        record = self.state_store.get_outbound("outbound-1")
        assert record is not None
        assert record.state == "provider_acknowledged"
        assert record.provider_message_id == "provider-message-1"
        assert [item.stage for item in self.state_store.delivery_receipts("outbound-1")] == [
            "gateway_accepted",
            "provider_submitted",
            "provider_acknowledged",
        ]
        assert [frame.payload["stage"] for frame in self.sink.frames] == [
            "gateway_accepted",
            "provider_submitted",
            "provider_acknowledged",
        ]
        assert [item.event for item in self.state_store.events_after(0)] == [
            "delivery.updated",
            "delivery.updated",
            "delivery.updated",
        ]

    async def test_sink_failure_never_changes_acknowledged_provider_state(self) -> None:
        failing_sink = _Sink(fail=True)
        manager = ChannelRuntimeManager(
            state_store=self.state_store,
            application_ingress=self.ingress,
            event_sink=failing_sink,
        )
        driver = _Driver("sink", ACCOUNT, self.log)
        manager.register(driver)
        await manager.start()
        await manager.activate()

        receipt = await manager.send(_outbound(outbound_id="sink-outbound"))

        assert receipt.stage == "provider_acknowledged"
        record = self.state_store.get_outbound("sink-outbound")
        assert record is not None
        assert record.state == "provider_acknowledged"
        assert manager.health().event_sink_failures == 3
        assert "subscriber secret" not in repr(manager.health())

    async def test_definite_rejection_is_failed_without_public_exception_detail(self) -> None:
        self.driver.send_error = ChannelDefinitelyNotSubmittedError(
            "provider_rejected",
            "provider secret response",
        )
        await self.manager.start()
        await self.manager.activate()

        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await self.manager.send(_outbound(outbound_id="rejected"))

        record = self.state_store.get_outbound("rejected")
        assert record is not None
        assert record.state == "failed"
        assert record.error_code == "provider_rejected"
        assert "provider secret response" not in repr(self.state_store.events_after(0))

    async def test_unknown_delivery_is_durable_and_duplicate_never_resends(self) -> None:
        self.driver.send_error = ChannelDeliveryUnknownError(
            "provider_ack_missing",
            "socket failed after bytes may have left",
        )
        await self.manager.start()
        await self.manager.activate()
        envelope = _outbound(outbound_id="unknown")

        with pytest.raises(ChannelDeliveryUnknownError):
            await self.manager.send(envelope)

        record = self.state_store.get_outbound("unknown")
        assert record is not None
        assert record.state == "delivery_unknown"
        assert record.error_code == "provider_ack_missing"
        replay = await self.manager.send(envelope)
        assert replay.stage == "delivery_unknown"
        assert self.driver.send_calls == 1

    async def test_unclassified_driver_failure_and_invalid_ack_are_delivery_unknown(self) -> None:
        self.driver.send_error = RuntimeError("provider implementation detail")
        await self.manager.start()
        await self.manager.activate()
        with pytest.raises(ChannelDeliveryUnknownError) as unexpected:
            await self.manager.send(_outbound(outbound_id="unexpected"))
        assert unexpected.value.code == "channel_delivery_exception"
        unexpected_record = self.state_store.get_outbound("unexpected")
        assert unexpected_record is not None
        assert unexpected_record.state == "delivery_unknown"

        self.driver.send_error = None
        self.driver.receipt = DeliveryReceipt(
            receipt_id="invalid-driver-receipt",
            outbound_id="wrong-outbound",
            stage="provider_acknowledged",
            observed_at=203.0,
        )
        with pytest.raises(ChannelDeliveryUnknownError) as invalid:
            await self.manager.send(_outbound(outbound_id="invalid-ack"))
        assert invalid.value.code == "channel_receipt_invalid"
        invalid_record = self.state_store.get_outbound("invalid-ack")
        assert invalid_record is not None
        assert invalid_record.state == "delivery_unknown"

    async def test_outbound_drift_stale_generation_and_missing_account_do_not_fallback(
        self,
    ) -> None:
        await self.manager.start()
        await self.manager.activate()
        await self.manager.send(_outbound(outbound_id="stable"))
        with pytest.raises(OutboundConflict):
            await self.manager.send(_outbound(outbound_id="stable", body="drifted"))
        assert self.driver.send_calls == 1

        with pytest.raises(ChannelDefinitelyNotSubmittedError) as missing:
            await self.manager.send(_outbound(outbound_id="missing", account=OTHER_ACCOUNT))
        assert missing.value.code == "channel_account_unavailable"
        missing_record = self.state_store.get_outbound("missing")
        assert missing_record is not None
        assert missing_record.state == "failed"
        assert self.driver.send_calls == 1

        self.state_store.acquire_writer_generation(now=600.0)
        with pytest.raises(StaleWriterGeneration):
            await self.manager.send(_outbound(outbound_id="stale"))
        assert self.driver.send_calls == 1
