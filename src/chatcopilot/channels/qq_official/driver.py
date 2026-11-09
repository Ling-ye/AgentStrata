"""Official QQ WebSocket lifecycle and text replies; no role or tool authorization."""
import asyncio
from collections import OrderedDict
import json
import time
from urllib.parse import urlsplit
import uuid

from websockets import connect

from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError, ChannelDeliveryUnknownError, ChannelHealth,
)
from chatcopilot.contracts.gateway import ChannelAccountRef, DeliveryReceipt
from .client import QQOfficialClient
from .codec import decode_event, outbound_request
from .config import QQOfficialError


class QQOfficialWebSocketDriver:
    def __init__(self, config, on_inbound, *, client=None, connection_factory=connect):
        self.config, self.on_inbound = config, on_inbound
        self.client = client or QQOfficialClient(config)
        self.connection_factory = connection_factory
        self._task = self._consumer = None
        self._queue = asyncio.Queue(maxsize=config.max_pending_events)
        self._state, self._code, self._generation = "stopped", None, None
        self._session, self._sequence = None, None
        self._contexts = OrderedDict()
        self._seen = OrderedDict()
        self._sent = set()

    @property
    def channel_id(self):
        return self.config.channel_id

    def health(self):
        return ChannelHealth(self.channel_id, ChannelAccountRef("qq_official", self.config.app_id),
                             self._state, self._generation, self._code)

    async def start(self):
        if self._task is not None:
            return
        self._state, self._code = "connecting", None
        self._consumer = asyncio.create_task(self._consume(), name="qq-official-events")
        self._task = asyncio.create_task(self._run(), name="qq-official-connection")

    async def stop(self):
        for task in (self._task, self._consumer):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._task, self._consumer) if t), return_exceptions=True)
        self._task = self._consumer = None
        self._queue = asyncio.Queue(maxsize=self.config.max_pending_events)
        self._session = self._sequence = self._generation = None
        self._contexts.clear()
        self._seen.clear()
        self._sent.clear()
        await self.client.close()
        self._state, self._code = "stopped", None

    async def _run(self):
        delay = 1
        while True:
            try:
                gateway = await self.client.request("GET", "/gateway")
                url = gateway.get("url")
                parsed = urlsplit(url or "")
                if (parsed.scheme != "wss" or parsed.hostname not in {
                        "api.bot.qq.com", "api.sgroup.qq.com", "sandbox.api.sgroup.qq.com"
                    } or parsed.username or parsed.password or parsed.port not in (None, 443)):
                    raise QQOfficialError("qq_official_gateway_invalid")
                token = await self.client.token()
                async with self.connection_factory(url, max_size=1024 * 1024,
                                                   open_timeout=20, close_timeout=5) as websocket:
                    await self._connected(websocket, token)
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._state = "connecting"
                self._code = error.code if isinstance(error, QQOfficialError) else "qq_official_connection_failed"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _connected(self, websocket, token):
        hello = json.loads(await asyncio.wait_for(websocket.recv(), 20))
        interval = hello.get("d", {}).get("heartbeat_interval")
        if hello.get("op") != 10 or type(interval) not in (int, float) or not 1000 <= interval <= 120000:
            raise QQOfficialError("qq_official_hello_invalid")
        self._generation = uuid.uuid4().hex
        if self._session and self._sequence is not None:
            auth = {"op": 6, "d": {"token": "QQBot " + token,
                                    "session_id": self._session, "seq": self._sequence}}
        else:
            auth = {"op": 2, "d": {"token": "QQBot " + token, "intents": 1 << 25,
                                    "shard": [0, 1], "properties": {}}}
        await websocket.send(json.dumps(auth))
        acknowledged = asyncio.Event()
        acknowledged.set()
        async def heartbeat():
            while True:
                await asyncio.sleep(interval / 1000)
                if not acknowledged.is_set():
                    await websocket.close()
                    return
                acknowledged.clear()
                await websocket.send(json.dumps({"op": 1, "d": self._sequence}))
        pulse = asyncio.create_task(heartbeat())
        try:
            while True:
                frame = json.loads(await asyncio.wait_for(websocket.recv(), interval / 1000 * 2 + 10))
                if not isinstance(frame, dict):
                    raise QQOfficialError("qq_official_frame_invalid")
                op = frame.get("op")
                if op == 11:
                    acknowledged.set()
                elif op == 1:
                    await websocket.send(json.dumps({"op": 1, "d": self._sequence}))
                elif op == 7:
                    return
                elif op == 9:
                    self._session = self._sequence = None
                    raise QQOfficialError("qq_official_session_invalid")
                elif op == 0:
                    event_type = frame.get("t")
                    if event_type == "READY":
                        session = frame.get("d", {}).get("session_id")
                        if not isinstance(session, str) or not session:
                            raise QQOfficialError("qq_official_ready_invalid")
                        self._session = session
                        self._state, self._code = "ready", None
                    elif event_type == "RESUMED":
                        self._state, self._code = "ready", None
                    elif self._state == "ready":
                        event = decode_event(frame, self.config.app_id, self._generation, time.time())
                        if event is not None:
                            try:
                                self._queue.put_nowait(event)
                            except asyncio.QueueFull:
                                # Keep the last queued sequence for resume. Never block
                                # heartbeat/control-frame intake behind application work.
                                raise QQOfficialError("qq_official_event_queue_full") from None
                    if type(frame.get("s")) is int:
                        self._sequence = frame["s"]
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            self._state = "connecting"

    async def _consume(self):
        while True:
            event = await self._queue.get()
            evidence = event.evidence
            key = (evidence.conversation.kind, evidence.conversation.conversation_id, evidence.message_id)
            try:
                if key in self._seen:
                    continue
                self._contexts[key] = evidence.observed_at
                await self.on_inbound(event)
                self._seen[key] = True
                while len(self._seen) > 4096:
                    self._seen.popitem(last=False)
                while len(self._contexts) > 4096:
                    oldest, _ = self._contexts.popitem(last=False)
                    self._sent.discard(oldest)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._contexts.pop(key, None)
                self._code = "qq_official_inbound_rejected"
            finally:
                self._queue.task_done()
            await asyncio.sleep(0)

    async def send(self, envelope):
        try:
            path, body = outbound_request(envelope, self.config.app_id)
            key = (envelope.conversation.kind, envelope.conversation.conversation_id, envelope.reply_to_message_id)
            observed = self._contexts.get(key)
            if (self._state != "ready" or observed is None or time.time() - observed > 240
                    or key in self._sent):
                raise QQOfficialError("qq_official_reply_context_unavailable")
        except QQOfficialError as error:
            raise ChannelDefinitelyNotSubmittedError(error.code, "QQ official reply is not valid") from None
        # Reserve before I/O: never repeat a reply whose provider outcome is unknown.
        self._sent.add(key)
        try:
            result = await self.client.request("POST", path, body)
        except QQOfficialError as error:
            if error.code in {"qq_official_http_rejected", "qq_official_api_rejected", "qq_official_auth_expired"}:
                self._sent.discard(key)
                raise ChannelDefinitelyNotSubmittedError(error.code, "QQ official rejected the reply") from None
            raise ChannelDeliveryUnknownError("qq_official_delivery_unknown", "QQ official delivery is unknown") from None
        except Exception:
            raise ChannelDeliveryUnknownError("qq_official_delivery_unknown", "QQ official delivery is unknown") from None
        message_id = result.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ChannelDeliveryUnknownError("qq_official_delivery_unknown", "QQ official acknowledgement is missing")
        return DeliveryReceipt(uuid.uuid4().hex, envelope.outbound_id, "provider_acknowledged",
                               time.time(), provider_message_id=message_id)


