from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

import pytest

from chatcopilot.channels.qq_onebot import driver as driver_module
from chatcopilot.channels.qq_onebot.config import OneBotChannelConfig
from chatcopilot.channels.qq_onebot.driver import (
    OneBotDefinitelyNotSubmittedError,
    OneBotDeliveryUnknownError,
    OneBotDriverError,
    OneBotForwardWebSocketDriver,
)
from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError,
    ChannelDeliveryUnknownError,
)
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    OutboundEnvelope,
)


BOT = "10001"
ACTOR = "20002"
GROUP = "30003"
TOKEN = "x" * 32


def test_onebot_delivery_errors_keep_generic_and_compatibility_types() -> None:
    rejected = OneBotDefinitelyNotSubmittedError("onebot_rejected", "rejected")
    unknown = OneBotDeliveryUnknownError("onebot_unknown", "unknown")

    assert rejected.code == "onebot_rejected"
    assert isinstance(rejected, ChannelDefinitelyNotSubmittedError)
    assert isinstance(rejected, OneBotDriverError)
    assert unknown.code == "onebot_unknown"
    assert isinstance(unknown, ChannelDeliveryUnknownError)
    assert isinstance(unknown, OneBotDriverError)


def _config(**changes: object) -> OneBotChannelConfig:
    values: dict[str, object] = {
        "channel_id": "qq-main",
        "account_id": BOT,
        "websocket_url": "ws://127.0.0.1:3001",
        "access_token": TOKEN,
        "action_timeout_seconds": 0.2,
        "max_frame_bytes": 256 * 1024,
    }
    values.update(changes)
    return OneBotChannelConfig(**values)  # type: ignore[arg-type]


def _group_event(text: str = "hello", *, group_id: str = GROUP) -> dict[str, object]:
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": "message-1",
        "group_id": group_id,
        "user_id": ACTOR,
        "sender": {"user_id": ACTOR, "nickname": "Actor"},
        "message": [
            {"type": "at", "data": {"qq": BOT}},
            {"type": "text", "data": {"text": text}},
        ],
    }


class _FakeConnection:
    def __init__(
        self,
        *,
        login_account: str = BOT,
        login_retcode: int = 0,
        pre_login_events: tuple[dict[str, object], ...] = (),
        reply_to_send: bool = True,
        send_retcode: int = 0,
        send_error: BaseException | None = None,
    ) -> None:
        self.login_account = login_account
        self.login_retcode = login_retcode
        self.pre_login_events = pre_login_events
        self.reply_to_send = reply_to_send
        self.send_retcode = send_retcode
        self.send_error = send_error
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        self.sent.append(request)
        if request["action"] == "get_login_info":
            for event in self.pre_login_events:
                await self.incoming.put(json.dumps(event))
            await self.incoming.put(
                json.dumps(
                    {
                        "status": "ok" if self.login_retcode == 0 else "failed",
                        "retcode": self.login_retcode,
                        "data": {"user_id": self.login_account},
                        "echo": request["echo"],
                    }
                )
            )
        elif request["action"] == "send_msg" and self.reply_to_send:
            if self.send_error is not None:
                raise self.send_error
            await self.incoming.put(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": "provider-message-9"},
                        "echo": "unrelated-echo",
                    }
                )
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "status": "ok" if self.send_retcode == 0 else "failed",
                        "retcode": self.send_retcode,
                        "data": {"message_id": "provider-message-9"},
                        "echo": request["echo"],
                    }
                )
            )

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def _outbound(*, account_id: str = BOT, text: str = "reply") -> OutboundEnvelope:
    return OutboundEnvelope(
        outbound_id="outbound-1",
        account=ChannelAccountRef(channel="qq", account_id=account_id),
        conversation=ConversationRef(kind="group", conversation_id=GROUP),
        segments=(MessageSegment(kind="text", text=text),),
        created_at=1.0,
    )


class OneBotDriverTests(IsolatedAsyncioTestCase):
    async def test_start_verifies_login_account_before_becoming_ready(self) -> None:
        connection = _FakeConnection()
        factory = mock.AsyncMock(return_value=connection)
        received: list[CanonicalInboundEvent] = []

        async def on_event(event: CanonicalInboundEvent) -> None:
            received.append(event)

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=factory,
        )
        await channel.start()

        assert channel.health().state == "ready"
        assert channel.health().account == ChannelAccountRef(channel="qq", account_id=BOT)
        assert channel.health().connection_generation is not None
        assert connection.sent[0]["action"] == "get_login_info"
        assert connection.sent[0]["params"] == {}
        assert received == []

        await channel.stop()
        assert channel.health().state == "stopped"
        assert connection.closed

    async def test_events_received_before_login_verification_are_not_emitted(self) -> None:
        connection = _FakeConnection(pre_login_events=(_group_event("too early"),))
        received: list[CanonicalInboundEvent] = []
        emitted = asyncio.Event()

        async def on_event(event: CanonicalInboundEvent) -> None:
            received.append(event)
            emitted.set()

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        await asyncio.sleep(0)
        assert received == []

        await connection.incoming.put(json.dumps(_group_event("after verification")))
        await asyncio.wait_for(emitted.wait(), timeout=1)
        assert len(received) == 1
        assert received[0].segments[-1].text == "after verification"
        await channel.stop()

    async def test_mismatched_login_account_fails_closed_and_closes_connection(self) -> None:
        connection = _FakeConnection(login_account="99999")

        async def on_event(_event: CanonicalInboundEvent) -> None:
            raise AssertionError("identity mismatch must not emit events")

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        with pytest.raises(OneBotDriverError) as caught:
            await channel.start()

        assert caught.value.code == "onebot_login_account_mismatch"
        assert channel.health().state == "error"
        assert channel.health().detail_code == "onebot_login_account_mismatch"
        assert connection.closed

    async def test_post_handshake_1403_is_treated_as_authentication_rejection(self) -> None:
        connection = _FakeConnection(login_retcode=1403)

        async def on_event(_event: CanonicalInboundEvent) -> None:
            raise AssertionError("authentication rejection must not emit events")

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        with pytest.raises(OneBotDriverError) as caught:
            await channel.start()

        assert caught.value.code == "onebot_authentication_rejected"
        assert channel.health().state == "error"
        assert connection.closed

    async def test_send_correlates_exact_echo_and_returns_provider_acknowledgement(self) -> None:
        connection = _FakeConnection()

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()

        receipt = await channel.send(_outbound())

        assert receipt.outbound_id == "outbound-1"
        assert receipt.stage == "provider_acknowledged"
        assert receipt.provider_message_id == "provider-message-9"
        request = connection.sent[-1]
        assert request["action"] == "send_msg"
        assert request["params"] == {
            "message_type": "group",
            "group_id": GROUP,
            "message": [{"type": "text", "data": {"text": "reply"}}],
        }
        assert receipt.detail == {"action": "send_msg", "retcode": 0}
        await channel.stop()

    async def test_send_timeout_is_delivery_unknown_and_is_not_reported_as_failure(self) -> None:
        connection = _FakeConnection(reply_to_send=False)

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(action_timeout_seconds=0.02),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()

        with pytest.raises(OneBotDeliveryUnknownError) as caught:
            await channel.send(_outbound())

        assert caught.value.code == "onebot_delivery_unknown"
        assert channel.health().state == "ready"
        await channel.stop()

    async def test_outbound_account_mismatch_is_rejected_before_action_submission(self) -> None:
        connection = _FakeConnection()

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        before = len(connection.sent)

        with pytest.raises(OneBotDefinitelyNotSubmittedError) as caught:
            await channel.send(_outbound(account_id="99999"))

        assert caught.value.code == "onebot_outbound_account_mismatch"
        assert len(connection.sent) == before
        await channel.stop()

    async def test_provider_rejection_is_definitely_not_submitted(self) -> None:
        connection = _FakeConnection(send_retcode=1200)

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()

        with pytest.raises(OneBotDefinitelyNotSubmittedError) as caught:
            await channel.send(_outbound())

        assert caught.value.code == "onebot_action_rejected"
        await channel.stop()

    async def test_oversized_outbound_frame_is_rejected_before_websocket_send(self) -> None:
        connection = _FakeConnection()

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(max_outbound_frame_bytes=1024),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        before = len(connection.sent)

        with pytest.raises(OneBotDefinitelyNotSubmittedError) as caught:
            await channel.send(_outbound(text="x" * 2000))

        assert caught.value.code == "onebot_outbound_frame_too_large"
        assert len(connection.sent) == before
        await channel.stop()

    async def test_send_exception_is_delivery_unknown_after_submission_begins(self) -> None:
        connection = _FakeConnection(send_error=RuntimeError("socket write failed"))

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()

        with pytest.raises(OneBotDeliveryUnknownError) as caught:
            await channel.send(_outbound())

        assert caught.value.code == "onebot_delivery_unknown"
        await channel.stop()

    async def test_reader_failure_after_send_is_delivery_unknown(self) -> None:
        connection = _FakeConnection(reply_to_send=False)

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        submission = asyncio.create_task(channel.send(_outbound()))
        for _ in range(20):
            if len(connection.sent) == 2:
                break
            await asyncio.sleep(0)
        assert len(connection.sent) == 2
        await connection.incoming.put(ConnectionError("socket closed after write"))

        with pytest.raises(OneBotDeliveryUnknownError) as caught:
            await submission

        assert caught.value.code == "onebot_delivery_unknown"
        assert channel.health().state == "error"
        await channel.stop()

    async def test_reader_failure_reauthenticates_before_accepting_reconnected_events(
        self,
    ) -> None:
        first = _FakeConnection()
        second = _FakeConnection()
        factory = mock.AsyncMock(side_effect=(first, second))
        received: list[CanonicalInboundEvent] = []
        emitted = asyncio.Event()

        async def on_event(event: CanonicalInboundEvent) -> None:
            received.append(event)
            emitted.set()

        channel = OneBotForwardWebSocketDriver(
            _config(
                reconnect_initial_seconds=0.01,
                reconnect_max_seconds=0.02,
            ),
            on_event,
            connection_factory=factory,
        )
        await channel.start()
        first_generation = channel.health().connection_generation

        await first.incoming.put(ConnectionError("provider disconnected"))
        for _ in range(100):
            health = channel.health()
            if health.state == "ready" and health.connection_generation != first_generation:
                break
            await asyncio.sleep(0.01)

        health = channel.health()
        assert health.state == "ready"
        assert health.connection_generation is not None
        assert health.connection_generation != first_generation
        assert factory.await_count == 2
        assert second.sent[0]["action"] == "get_login_info"
        await second.incoming.put(json.dumps(_group_event("after reconnect")))
        await asyncio.wait_for(emitted.wait(), timeout=1)
        assert received[-1].segments[-1].text == "after reconnect"
        assert received[-1].evidence.connection_generation == health.connection_generation
        await channel.stop()

    async def test_stop_cancels_pending_reconnect(self) -> None:
        first = _FakeConnection()
        second = _FakeConnection()
        factory = mock.AsyncMock(side_effect=(first, second))

        async def on_event(_event: CanonicalInboundEvent) -> None:
            return None

        channel = OneBotForwardWebSocketDriver(
            _config(
                reconnect_initial_seconds=0.05,
                reconnect_max_seconds=0.05,
            ),
            on_event,
            connection_factory=factory,
        )
        await channel.start()
        await first.incoming.put(ConnectionError("provider disconnected"))
        for _ in range(50):
            if channel.health().state == "error":
                break
            await asyncio.sleep(0.001)

        await channel.stop()
        await asyncio.sleep(0.06)

        assert channel.health().state == "stopped"
        assert factory.await_count == 1

    async def test_blocked_event_handler_does_not_block_action_response_demux(self) -> None:
        connection = _FakeConnection()
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def on_event(_event: CanonicalInboundEvent) -> None:
            handler_started.set()
            await release_handler.wait()

        channel = OneBotForwardWebSocketDriver(
            _config(action_timeout_seconds=0.05),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        await connection.incoming.put(json.dumps(_group_event()))
        await asyncio.wait_for(handler_started.wait(), timeout=1)

        receipt = await channel.send(_outbound())

        assert receipt.stage == "provider_acknowledged"
        assert receipt.provider_message_id == "provider-message-9"
        release_handler.set()
        await channel.stop()

    async def test_ingress_queue_overflow_fails_connection_closed(self) -> None:
        connection = _FakeConnection()
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def on_event(_event: CanonicalInboundEvent) -> None:
            handler_started.set()
            await release_handler.wait()

        channel = OneBotForwardWebSocketDriver(
            _config(max_pending_events=1),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        await connection.incoming.put(json.dumps(_group_event("first")))
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        await connection.incoming.put(json.dumps(_group_event("second")))
        await connection.incoming.put(json.dumps(_group_event("overflow")))

        for _ in range(20):
            if channel.health().state == "error":
                break
            await asyncio.sleep(0.01)

        assert channel.health().state == "error"
        assert channel.health().detail_code == "onebot_ingress_queue_full"
        assert connection.closed
        release_handler.set()
        await channel.stop()

    async def test_event_handler_failure_does_not_disconnect_transport(self) -> None:
        connection = _FakeConnection()
        attempts = 0
        handled = asyncio.Event()

        async def on_event(_event: CanonicalInboundEvent) -> None:
            nonlocal attempts
            attempts += 1
            handled.set()
            raise RuntimeError("gateway persistence unavailable")

        channel = OneBotForwardWebSocketDriver(
            _config(),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        await connection.incoming.put(json.dumps(_group_event()))
        await asyncio.wait_for(handled.wait(), timeout=1)
        await asyncio.sleep(0)

        assert attempts == 1
        assert channel.health().state == "ready"
        assert channel.health().detail_code is None
        assert not connection.closed
        await channel.stop()

    async def test_different_events_can_use_bounded_parallel_workers(self) -> None:
        connection = _FakeConnection()
        both_started = asyncio.Event()
        release = asyncio.Event()
        started = 0

        async def on_event(_event: CanonicalInboundEvent) -> None:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await release.wait()

        channel = OneBotForwardWebSocketDriver(
            _config(max_pending_events=2),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        await connection.incoming.put(json.dumps(_group_event("first")))
        await connection.incoming.put(json.dumps(_group_event("second", group_id="40004")))

        await asyncio.wait_for(both_started.wait(), timeout=1)
        assert channel.health().state == "ready"
        release.set()
        await channel.stop()

    async def test_same_conversation_events_remain_ordered(self) -> None:
        connection = _FakeConnection()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        observed: list[str] = []

        async def on_event(event: CanonicalInboundEvent) -> None:
            text = event.segments[-1].text
            assert text is not None
            observed.append(text)
            if text == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()

        channel = OneBotForwardWebSocketDriver(
            _config(max_pending_events=2),
            on_event,
            connection_factory=mock.AsyncMock(return_value=connection),
        )
        await channel.start()
        await connection.incoming.put(json.dumps(_group_event("first")))
        await connection.incoming.put(json.dumps(_group_event("second")))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.sleep(0.01)

        assert not second_started.is_set()
        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert observed == ["first", "second"]
        await channel.stop()

    async def test_default_connector_always_sends_bearer_token_and_frame_limit(self) -> None:
        connection = _FakeConnection()
        connect = mock.AsyncMock(return_value=connection)
        websockets = SimpleNamespace(connect=connect)

        with mock.patch.dict(sys.modules, {"websockets": websockets}):
            result = await driver_module._connect_websocket(_config())

        assert result is connection
        awaited = connect.await_args
        assert awaited is not None
        assert awaited.args == ("ws://127.0.0.1:3001",)
        kwargs = awaited.kwargs
        assert kwargs["additional_headers"] == {"Authorization": f"Bearer {TOKEN}"}
        assert kwargs["max_size"] == 256 * 1024
        assert kwargs["open_timeout"] == 0.2
