"""Trusted image/network and OneBot wire fixtures; product handlers remain real."""
from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
import io
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

from chatcopilot.application.file_delivery import create_file_sender
from chatcopilot.channels.qq_onebot import OneBotChannelConfig, OneBotForwardWebSocketDriver
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef, OutboundEnvelope
from chatcopilot.gateway.channels import ChannelRuntimeManager
from chatcopilot.gateway.state_store import GatewayStateStore


def validate_fixtures(value):
    if not isinstance(value, dict) or set(value) - {"http", "delivery"}:
        raise ValueError("invalid external dependency fixtures")
    responses = value.get("http", {})
    if not isinstance(responses, dict):
        raise ValueError("http fixtures must be a URL map")
    for url, row in responses.items():
        if not isinstance(url, str):
            raise ValueError("invalid fixture URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid fixture URL")
        if not isinstance(row, dict) or set(row) - {"status", "content_type", "body_base64", "location"}:
            raise ValueError("invalid HTTP fixture")
        if type(row.get("status", 200)) is not int or not 100 <= row.get("status", 200) <= 599:
            raise ValueError("invalid HTTP fixture status")
        if not isinstance(row.get("body_base64", ""), str):
            raise ValueError("invalid HTTP fixture body")
        base64.b64decode(row.get("body_base64", ""), validate=True)
        if any(not isinstance(row.get(k, ""), str) for k in ("content_type", "location")):
            raise ValueError("invalid HTTP fixture headers")
    if value.get("delivery", "acknowledged") not in ("acknowledged", "rejected", "unknown", "incomplete"):
        raise ValueError("invalid OneBot fixture outcome")
    return value


class OneBotFixtureConnection:
    """Replace only the websocket; use the real driver, codec and ChannelRuntime."""
    def __init__(self, outcome="acknowledged"):
        self.queue = asyncio.Queue()
        self.outcome = outcome
        self.actions = []

    async def send(self, raw):
        request = json.loads(raw)
        self.actions.append(request)
        if request["action"] == "get_login_info":
            data, retcode = {"user_id": "10001"}, 0
        else:
            if self.outcome == "unknown":
                return
            data = {} if self.outcome == "incomplete" else {"message_id": str(len(self.actions))}
            retcode = 1200 if self.outcome == "rejected" else 0
        await self.queue.put(json.dumps({"echo": request["echo"], "status": "ok" if retcode == 0 else "failed",
                                        "retcode": retcode, "data": data}))

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        return None


class ImageDeliveryFixture:
    def __init__(self, root: Path, workspace, declaration):
        self.root, self.workspace = root, workspace
        self.spec = validate_fixtures(declaration.get("external_fixtures", {}))
        self.enabled = bool(declaration.get("external_fixtures"))
        self.channel_kind = declaration["channel_kind"]
        self.stack = ExitStack()
        self.sender = None
        self.receipts = []
        self.connection = None
        self.loop = None

    def __enter__(self):
        if not self.enabled:
            return self
        responses = self.spec.get("http", {})

        class Connection:
            def __init__(self, host, port, address):
                self.host, self.port = host, port
            def request(self, method, path, headers):
                matches = [row for url, row in responses.items()
                           if urlsplit(url).hostname == self.host and (urlsplit(url).port or
                           (443 if urlsplit(url).scheme == "https" else 80)) == self.port
                           and (urlsplit(url).path or "/") + ("?" + urlsplit(url).query if urlsplit(url).query else "") == path]
                if len(matches) != 1:
                    raise ConnectionError("No frozen HTTP response for this URL")
                self.row = matches[0]
            def getresponse(self):
                body = io.BytesIO(base64.b64decode(self.row.get("body_base64", ""), validate=True))
                headers = {"Content-Type": self.row.get("content_type", "image/png"),
                           "Content-Length": str(len(body.getvalue())), "Location": self.row.get("location", "")}
                return SimpleNamespace(status=self.row.get("status", 200), getheader=headers.get, read=body.read)
            def close(self):
                pass

        # DNS/HTTP are dependency boundaries; URL, address, redirect and image validators execute unchanged.
        hosts = {urlsplit(url).hostname for url in responses}
        def resolve(host, port, *args, **kwargs):
            if host in hosts:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            raise socket.gaierror("No frozen image DNS response")
        self.stack.enter_context(patch("chatcopilot.agent.tools.builtin.workspace.images._resolve_image_dns", resolve))
        self.stack.enter_context(patch("chatcopilot.agent.tools.builtin.workspace.images._PinnedHTTPConnection", Connection))
        self.stack.enter_context(patch("chatcopilot.agent.tools.builtin.workspace.images._PinnedHTTPSConnection", Connection))
        try:
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
            self.thread.start()
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=5)
            def dispatch(segments):
                future = asyncio.run_coroutine_threadsafe(self._send(segments), self.loop)
                try:
                    return future.result(timeout=5)
                except BaseException:
                    future.cancel()
                    raise
            self.sender = create_file_sender(self.workspace, dispatch)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    async def _start(self):
        self.connection = OneBotFixtureConnection(self.spec.get("delivery", "acknowledged"))
        self.state = GatewayStateStore(self.root / "delivery-state")
        self.runtime = ChannelRuntimeManager(state_store=self.state, gateway_ingress=SimpleNamespace())
        async def connect(config):
            return self.connection
        driver = OneBotForwardWebSocketDriver(
            OneBotChannelConfig("fixture", "10001", "ws://127.0.0.1:1", "f" * 32, action_timeout_seconds=.2),
            self.runtime.handle_inbound, connection_factory=connect)
        self.runtime.register(driver)
        await self.runtime.start()
        await self.runtime.activate()

    async def _send(self, segments):
        envelope = OutboundEnvelope("outbound_" + uuid.uuid4().hex, ChannelAccountRef("qq", "10001"),
            ConversationRef(self.channel_kind, "30003"), segments, time.time())
        receipt = await self.runtime.send(envelope)
        self.receipts.append({"outbound_id": envelope.outbound_id, "stage": receipt.stage,
                              "provider_message_id": receipt.provider_message_id,
                              "segments": [segment.kind for segment in segments]})
        return receipt

    def evidence(self):
        return {"kind": "isolated_image_delivery", "production_delivery": False, "receipts": self.receipts,
                "actions": [a for a in (self.connection.actions if self.connection else []) if a["action"] != "get_login_info"]}

    def __exit__(self, *_args):
        try:
            if self.loop is not None:
                if hasattr(self, "runtime"):
                    asyncio.run_coroutine_threadsafe(self.runtime.stop(), self.loop).result(timeout=5)
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=5)
                self.loop.close()
        finally:
            self.stack.close()
