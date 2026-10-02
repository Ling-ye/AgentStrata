"""iLink lifecycle and actual delivery; authorization remains in Gateway."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid

from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError,
    ChannelDeliveryUnknownError,
    ChannelHealth,
    InboundEventHandler,
)
from chatcopilot.contracts.gateway import ChannelAccountRef, DeliveryReceipt, OutboundEnvelope
from .client import WeixinClient
from .codec import decode_message
from .state import WeixinState
from chatcopilot.contracts.weixin import WeixinChannelConfig, WeixinError


_LOG = logging.getLogger(__name__)


class WeixinDriver:
    def __init__(
        self,
        config: WeixinChannelConfig,
        on_inbound: InboundEventHandler,
        *,
        state: WeixinState,
        client: WeixinClient | None = None,
    ):
        self.config, self.on_inbound, self.state = config, on_inbound, state
        self.client = client or WeixinClient(
            base_url=config.base_url, token=config.token, max_frame_bytes=config.max_frame_bytes
        )
        self._task: asyncio.Task | None = None
        self._generation: str | None = None
        self._status = "stopped"
        self._code: str | None = None

    @property
    def channel_id(self) -> str:
        return self.config.channel_id

    def health(self) -> ChannelHealth:
        return ChannelHealth(
            self.channel_id,
            ChannelAccountRef("weixin", self.config.account_id),
            self._status,
            self._generation,
            self._code,
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._status = "connecting"
        try:
            first = await self.client.request(
                "getupdates",
                {"get_updates_buf": self.state.cursor()},
                timeout=self.config.long_poll_timeout_seconds + 10,
            )
            if (
                type(first.get("ret")) is not int
                or first["ret"] != 0
                or not isinstance(first.get("msgs", []), list)
            ):
                raise WeixinError("weixin_updates_invalid")
        except BaseException:
            self._status = "error"
            self._code = "weixin_connect_failed"
            await self.client.close()
            raise
        self._generation = uuid.uuid4().hex
        self._status, self._code = "ready", None
        self._task = asyncio.create_task(self._poll(first), name="weixin-ilink-reader")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.client.close()
        self._status, self._generation = "stopped", None

    async def _poll(self, first: dict | None = None) -> None:
        delay = 1.0
        while True:
            try:
                result = (
                    first
                    if first is not None
                    else await self.client.request(
                        "getupdates",
                        {"get_updates_buf": self.state.cursor()},
                        timeout=self.config.long_poll_timeout_seconds + 10,
                    )
                )
                first = None
                messages = result.get("msgs", [])
                if not isinstance(messages, list):
                    raise WeixinError("weixin_updates_invalid")
                self._status = "ready"
                for message in messages:
                    await self._accept(message)
                cursor = result.get("get_updates_buf")
                if isinstance(cursor, str) and cursor:
                    self.state.set_cursor(cursor)
                delay, self._code = 1.0, None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = error.code if isinstance(error, WeixinError) else "weixin_intake_failed"
                if code == "weixin_poll_timeout":
                    continue
                self._code = code
                if code == "weixin_auth_expired":
                    self._status = "error"
                    return
                self._status = "connecting"
                _LOG.warning("Weixin channel: %s", code)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _accept(self, message: dict) -> None:
        try:
            event = decode_message(message, self.config, self._generation or "", time.time())
        except WeixinError as error:
            _LOG.warning("Weixin message rejected: %s", error.code)
            return
        if event is None:
            return
        try:
            await self.on_inbound(event)
        except Exception as error:
            if getattr(error, "code", "") in {
                "weixin-sender-not-allowed",
                "weixin-chat-kind-invalid",
                "weixin-account-invalid",
            }:
                return
            raise
        token = message.get("context_token")
        if isinstance(token, str) and token:
            # No suspension between durable acceptance and reply-state persistence.
            self.state.set_context(event.evidence.conversation.conversation_id, token)

    async def send(self, envelope: OutboundEnvelope) -> DeliveryReceipt:
        if (
            self._status != "ready"
            or envelope.account != self.health().account
            or envelope.conversation.kind != "p2p"
            or envelope.conversation.conversation_id != self.config.user_id
            or len(envelope.segments) != 1
        ):
            raise ChannelDefinitelyNotSubmittedError(
                "weixin_outbound_binding_invalid", "Weixin delivery binding is invalid"
            )
        peer = envelope.conversation.conversation_id
        token = self.state.context(peer)
        if not token:
            raise ChannelDefinitelyNotSubmittedError(
                "weixin_reply_context_missing", "Send a message to refresh Weixin reply context"
            )
        segment = envelope.segments[0]
        try:
            if segment.kind == "text":
                item = {"type": 1, "text_item": {"text": segment.text or ""}}
            elif segment.kind in ("image", "file"):
                source = segment.data.get("source", "")
                if not isinstance(source, str) or not source.startswith("base64://"):
                    raise WeixinError("weixin_outbound_resource_invalid")
                data = base64.b64decode(source[9:], validate=True)
                item = await self.client.upload(
                    data,
                    user_id=peer,
                    kind=segment.kind,
                    name=str(segment.data.get("name", "file")),
                )
            else:
                raise WeixinError("weixin_outbound_kind_unsupported")
        except (WeixinError, ValueError) as error:
            raise ChannelDefinitelyNotSubmittedError(
                getattr(error, "code", "weixin_outbound_resource_invalid"),
                "Weixin message was not submitted",
            ) from None
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": peer,
                "client_id": envelope.outbound_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": token,
                "item_list": [item],
            }
        }
        try:
            result = await self.client.request(
                "sendmessage", body, timeout=self.config.action_timeout_seconds
            )
        except WeixinError as error:
            if error.code in ("weixin_api_rejected", "weixin_auth_expired"):
                raise ChannelDefinitelyNotSubmittedError(
                    error.code, "Weixin rejected message submission"
                ) from None
            raise ChannelDeliveryUnknownError(
                error.code, "Weixin message submission was not confirmed"
            ) from None
        if type(result.get("ret")) is not int or result["ret"] != 0:
            raise ChannelDeliveryUnknownError(
                "weixin_ack_invalid", "Weixin message submission was not confirmed"
            )
        provider_id = result.get("message_id")
        if provider_id is not None and (isinstance(provider_id, bool) or not isinstance(provider_id, (str, int))):
            raise ChannelDeliveryUnknownError(
                "weixin_ack_invalid", "Weixin acknowledgement is invalid"
            )
        return DeliveryReceipt(
            "receipt_" + uuid.uuid4().hex,
            envelope.outbound_id,
            "provider_acknowledged",
            time.time(),
            provider_message_id=str(provider_id) if provider_id is not None else None,
        )
