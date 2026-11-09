from __future__ import annotations

import asyncio
import json
import time

import pytest

from chatcopilot.channels.base import ChannelDefinitelyNotSubmittedError, ChannelDeliveryUnknownError
from chatcopilot.channels.qq_official import QQOfficialChannelConfig, QQOfficialWebSocketDriver
from chatcopilot.channels.qq_official.config import QQOfficialError
from chatcopilot.channels.qq_official.codec import decode_event, outbound_request
from chatcopilot.channels.qq_official.client import QQOfficialClient
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef, MessageSegment, OutboundEnvelope


def config():
    return QQOfficialChannelConfig("qq-official", "12345", "test-secret")


def frame(kind="GROUP_AT_MESSAGE_CREATE"):
    group = kind.startswith("GROUP")
    return {"op": 0, "s": 4, "t": kind, "d": {
        "id": "ROBOT1.0_msg", "group_openid": "group-1", "content": " hello ",
        "author": {"member_openid" if group else "user_openid": "user-1"},
    }}


def envelope(**changes):
    values = dict(outbound_id="out-1", account=ChannelAccountRef("qq_official", "12345"),
                  conversation=ConversationRef("group", "group-1"),
                  segments=(MessageSegment("text", text="reply"),), created_at=time.time(),
                  reply_to_message_id="ROBOT1.0_msg")
    values.update(changes)
    return OutboundEnvelope(**values)


def test_config_and_codec():
    assert "test-secret" not in repr(config())
    event = decode_event(frame(), "12345", "generation", 10)
    assert event.evidence.sender.sender_id == "user-1"
    assert event.evidence.account.channel == "qq_official"
    assert event.evidence.conversation == ConversationRef("group", "group-1")
    assert event.segments[0].text == "hello"
    assert decode_event({"t": "MESSAGE_CREATE"}, "12345", "g", 10) is None
    private = decode_event(frame("C2C_MESSAGE_CREATE"), "12345", "g", 10)
    assert private.evidence.conversation == ConversationRef("p2p", "user-1")
    with pytest.raises(QQOfficialError):
        decode_event({"t": "GROUP_AT_MESSAGE_CREATE", "d": {}}, "12345", "g", 10)


def test_outbound_fails_closed():
    path, body = outbound_request(envelope(), "12345")
    assert path == "/v2/groups/group-1/messages"
    assert body["msg_id"] == "ROBOT1.0_msg"
    assert body["msg_seq"] == 1
    for changes in (
        {"account": ChannelAccountRef("qq", "12345")},
        {"reply_to_message_id": None},
        {"segments": (MessageSegment("image"),)},
        {"segments": (MessageSegment("text", text="x" * 4001),)},
    ):
        with pytest.raises(QQOfficialError):
            outbound_request(envelope(**changes), "12345")


class Client:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def request(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return {"id": "reply-id"}

    async def token(self):
        return "token"

    async def close(self):
        pass


def test_delivery_and_unknown_are_not_replayed():
    async def check():
        client = Client()
        driver = QQOfficialWebSocketDriver(config(), None, client=client)
        driver._state = "ready"
        key = ("group", "group-1", "ROBOT1.0_msg")
        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await driver.send(envelope())
        driver._contexts[key] = time.time()
        receipt = await driver.send(envelope())
        assert receipt.stage == "provider_acknowledged"
        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await driver.send(envelope())
        assert len(client.calls) == 1
        driver._sent.clear()
        client.error = QQOfficialError("qq_official_transport_unknown")
        with pytest.raises(ChannelDeliveryUnknownError):
            await driver.send(envelope())
        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await driver.send(envelope())
        driver._sent.clear()
        driver._contexts[key] = time.time() - 241
        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await driver.send(envelope())
    asyncio.run(check())


def test_token_is_cached_and_refreshed():
    async def check():
        client = QQOfficialClient(config())
        calls = []
        def request(*args):
            calls.append(args)
            return {"access_token": "fixture", "expires_in": 7200}
        client._request = request
        assert await client.token() == "fixture"
        assert await client.token() == "fixture"
        assert len(calls) == 1
        client._expires = 0
        await client.token()
        assert len(calls) == 2
        await client.close()
        assert not client._token
    asyncio.run(check())


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def recv(self):
        return json.dumps(next(self.frames))

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self):
        pass


def test_handshake_and_session_resume():
    async def check():
        driver = QQOfficialWebSocketDriver(config(), None, client=Client())
        socket = Socket([{"op": 10, "d": {"heartbeat_interval": 30000}},
                         {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "session"}},
                         frame(), {"op": 7}])
        await driver._connected(socket, "fixture-token")
        assert socket.sent[0]["op"] == 2
        assert socket.sent[0]["d"]["intents"] == 1 << 25
        assert driver._sequence == 4
        assert driver._queue.qsize() == 1
        socket = Socket([{"op": 10, "d": {"heartbeat_interval": 30000}},
                         {"op": 0, "t": "RESUMED", "s": 5}, {"op": 7}])
        await driver._connected(socket, "fixture-token")
        assert socket.sent[0]["op"] == 6
        assert socket.sent[0]["d"]["seq"] == 4
        assert socket.sent[0]["d"]["session_id"] == "session"
    asyncio.run(check())


def test_consumer_deduplicates_and_does_not_block_reader():
    async def check():
        received = []
        async def inbound(event):
            received.append(event)
        driver = QQOfficialWebSocketDriver(config(), inbound, client=Client())
        event = decode_event(frame(), "12345", "g", time.time())
        task = asyncio.create_task(driver._consume())
        await driver._queue.put(event)
        await driver._queue.put(event)
        await driver._queue.join()
        assert len(received) == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


def test_token_auth_failure_clears_cache_without_retry():
    async def check():
        client = QQOfficialClient(config())
        client._token, client._expires = "cached", time.monotonic() + 3600
        calls = []
        def fail(*args):
            calls.append(args)
            raise QQOfficialError("qq_official_auth_expired")
        client._request = fail
        with pytest.raises(QQOfficialError):
            await client.request("GET", "/gateway")
        assert len(calls) == 1
        assert client._token == ""
    asyncio.run(check())


def test_path_ids_cannot_traverse():
    with pytest.raises(QQOfficialError):
        outbound_request(envelope(conversation=ConversationRef("group", "..")), "12345")


def test_rejected_events_are_not_retained():
    async def check():
        async def inbound(event):
            raise ValueError("denied")
        driver = QQOfficialWebSocketDriver(config(), inbound, client=Client())
        task = asyncio.create_task(driver._consume())
        await driver._queue.put(decode_event(frame(), "12345", "g", time.time()))
        await driver._queue.join()
        assert not driver._contexts and not driver._seen
        assert driver.health().detail_code == "qq_official_inbound_rejected"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


def test_invalid_session_resets_resume():
    async def check():
        driver = QQOfficialWebSocketDriver(config(), None, client=Client())
        driver._session, driver._sequence = "old", 6
        socket = Socket([{"op": 10, "d": {"heartbeat_interval": 30000}}, {"op": 9}])
        with pytest.raises(QQOfficialError, match="session_invalid"):
            await driver._connected(socket, "token")
        assert driver._session is None and driver._sequence is None
    asyncio.run(check())


@pytest.mark.parametrize("status,code", [
    (401, "qq_official_auth_expired"),
    (403, "qq_official_http_rejected"),
    (500, "qq_official_transport_unknown"),
    (302, "qq_official_transport_unknown"),
])
def test_http_errors_do_not_disclose_response_or_retry(monkeypatch, status, code):
    from chatcopilot.channels.qq_official import client as module
    class Response:
        status_code = status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Session:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, *args, **kwargs):
            assert self.trust_env is False
            assert kwargs["allow_redirects"] is False
            return Response()
    monkeypatch.setattr(module.requests, "Session", Session)
    with pytest.raises(QQOfficialError, match=code):
        QQOfficialClient(config())._request("POST", module.API_BASE + "/test")


def test_definite_rejection_can_retry_but_unknown_cannot():
    async def check():
        for code in ("qq_official_http_rejected", "qq_official_api_rejected", "qq_official_auth_expired"):
            client = Client(QQOfficialError(code))
            driver = QQOfficialWebSocketDriver(config(), None, client=client)
            driver._state = "ready"
            key = ("group", "group-1", "ROBOT1.0_msg")
            driver._contexts[key] = time.time()
            with pytest.raises(ChannelDefinitelyNotSubmittedError):
                await driver.send(envelope())
            assert key not in driver._sent
            client.error = None
            receipt = await driver.send(envelope())
            assert receipt.stage == "provider_acknowledged"
            assert len(client.calls) == 2
            assert key in driver._sent
    asyncio.run(check())

def test_full_queue_disconnects_without_waiting_or_advancing_sequence():
    async def check():
        cfg = QQOfficialChannelConfig("qq-official", "12345", "test-secret", max_pending_events=1)
        driver = QQOfficialWebSocketDriver(cfg, None, client=Client())
        second = frame()
        second["s"] = 5
        second["d"]["id"] = "second-message"
        socket = Socket([{"op": 10, "d": {"heartbeat_interval": 30000}},
                         {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "session"}},
                         frame(), second])
        with pytest.raises(QQOfficialError, match="event_queue_full"):
            await asyncio.wait_for(driver._connected(socket, "fixture-token"), 1)
        assert driver._queue.qsize() == 1
        assert driver._sequence == 4
        assert driver._session == "session"
        assert driver.health().state == "connecting"
    asyncio.run(check())
