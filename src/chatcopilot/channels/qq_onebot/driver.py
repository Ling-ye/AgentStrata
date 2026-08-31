"""Authenticated forward-WebSocket lifecycle for OneBot v11 providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import hashlib
import logging
import secrets
import time
from typing import Any, Protocol

from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError,
    ChannelDeliveryUnknownError,
    ChannelHealth,
    ChannelState,
    InboundEventHandler,
)
from chatcopilot.channels.qq_onebot.codec import (
    OneBotActionResponse,
    OneBotCodecError,
    build_outbound_action,
    decode_action_response,
    decode_inbound_message,
    encode_action_request,
    parse_native_frame,
)
from chatcopilot.channels.qq_onebot.config import OneBotChannelConfig
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    DeliveryReceipt,
    OutboundEnvelope,
)


class OneBotConnection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[OneBotChannelConfig], Awaitable[OneBotConnection]]


class OneBotDriverError(RuntimeError):
    """Stable, secret-free Channel driver failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OneBotDeliveryUnknownError(ChannelDeliveryUnknownError, OneBotDriverError):
    """The provider may have applied an action whose response was not observed."""


class OneBotDefinitelyNotSubmittedError(
    ChannelDefinitelyNotSubmittedError,
    OneBotDriverError,
):
    """The observed failure proves no outbound message was accepted by the provider."""


_LOGGER = logging.getLogger(__name__)
_MAX_EVENT_WORKERS = 8
_EVENT_LANE_COUNT = 64


class OneBotForwardWebSocketDriver:
    """Own one authenticated OneBot connection and correlate every action reply."""

    def __init__(
        self,
        config: OneBotChannelConfig,
        on_event: InboundEventHandler,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._connection_factory = connection_factory or _connect_websocket
        self._account = ChannelAccountRef(channel="qq", account_id=config.account_id)
        self._state: ChannelState = "stopped"
        self._detail_code: str | None = None
        self._connection_generation: str | None = None
        self._connection: OneBotConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._event_tasks: tuple[asyncio.Task[None], ...] = ()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._event_queue: asyncio.Queue[CanonicalInboundEvent] = asyncio.Queue(
            maxsize=config.max_pending_events
        )
        self._pending: dict[str, asyncio.Future[OneBotActionResponse]] = {}
        self._event_lanes = tuple(asyncio.Lock() for _ in range(_EVENT_LANE_COUNT))
        self._lifecycle_lock = asyncio.Lock()
        self._ready = False
        self._desired_running = False
        self._echo_counter = 0

    @property
    def channel_id(self) -> str:
        return self._config.channel_id

    async def start(self) -> None:
        """Connect and verify ``get_login_info`` before accepting any message event."""

        async with self._lifecycle_lock:
            if self._state == "ready" and self._ready:
                return
            self._desired_running = True
            await self._cancel_reconnect_locked()
            await self._disconnect_locked()
            try:
                await self._connect_locked()
            except asyncio.CancelledError:
                self._desired_running = False
                await self._disconnect_locked()
                self._state = "stopped"
                raise
            except Exception as exc:
                self._desired_running = False
                error = _normalize_driver_error(exc, default_code="onebot_start_failed")
                self._detail_code = error.code
                self._state = "error"
                await self._disconnect_locked()
                raise error from exc

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._desired_running = False
            await self._cancel_reconnect_locked()
            self._ready = False
            await self._disconnect_locked()
            self._state = "stopped"
            self._detail_code = None
            self._connection_generation = None

    def health(self) -> ChannelHealth:
        return ChannelHealth(
            channel_id=self.channel_id,
            account=self._account,
            state=self._state,
            connection_generation=self._connection_generation,
            detail_code=self._detail_code,
        )

    async def send(self, envelope: OutboundEnvelope) -> DeliveryReceipt:
        if not self._ready or self._state != "ready":
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_not_ready",
                "OneBot Channel is not ready",
            )
        if envelope.account != self._account:
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_outbound_account_mismatch",
                "Outbound account does not match the connected OneBot account",
            )
        if (
            not envelope.outbound_id
            or len(envelope.outbound_id) > 256
            or not all(character.isprintable() for character in envelope.outbound_id)
        ):
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_outbound_id_invalid",
                "Outbound envelope requires a bounded outbound_id",
            )
        try:
            action, params = build_outbound_action(envelope)
        except OneBotCodecError as exc:
            raise OneBotDefinitelyNotSubmittedError(exc.code, str(exc)) from exc
        response = await self._request(action, params, side_effect_unknown=True)
        provider_message_id = _provider_message_id(response.data)
        return DeliveryReceipt(
            receipt_id=f"receipt_{secrets.token_hex(16)}",
            outbound_id=envelope.outbound_id,
            stage="provider_acknowledged",
            observed_at=time.time(),
            provider_message_id=provider_message_id,
            detail={"action": action, "retcode": response.retcode},
        )

    async def _connect_locked(self) -> None:
        """Establish and authenticate one new provider connection generation."""

        self._state = "connecting"
        self._detail_code = None
        self._connection_generation = f"connection_{secrets.token_hex(16)}"
        connection = await self._connection_factory(self._config)
        self._connection = connection
        response = await self._verify_login(connection)
        actual_account = _login_account(response.data)
        if actual_account != self._config.account_id:
            raise OneBotDriverError(
                "onebot_login_account_mismatch",
                "OneBot get_login_info account does not match the configured account",
            )
        self._ready = True
        self._event_queue = asyncio.Queue(maxsize=self._config.max_pending_events)
        worker_count = min(_MAX_EVENT_WORKERS, self._config.max_pending_events)
        self._event_tasks = tuple(
            asyncio.create_task(
                self._event_worker_loop(connection, self._event_queue),
                name=f"onebot-events-{self._config.channel_id}-{index}",
            )
            for index in range(worker_count)
        )
        self._reader_task = asyncio.create_task(
            self._reader_loop(connection),
            name=f"onebot-reader-{self._config.channel_id}",
        )
        self._state = "ready"

    async def _cancel_reconnect_locked(self) -> None:
        task = self._reconnect_task
        self._reconnect_task = None
        if task is None or task is asyncio.current_task() or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _schedule_reconnect(self) -> None:
        task = self._reconnect_task
        if not self._desired_running or (task is not None and not task.done()):
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(),
            name=f"onebot-reconnect-{self._config.channel_id}",
        )

    async def _reconnect_loop(self) -> None:
        delay = self._config.reconnect_initial_seconds
        try:
            while self._desired_running:
                await asyncio.sleep(delay)
                async with self._lifecycle_lock:
                    if not self._desired_running:
                        return
                    await self._disconnect_locked()
                    try:
                        await self._connect_locked()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        error = _normalize_driver_error(
                            exc,
                            default_code="onebot_reconnect_failed",
                        )
                        self._ready = False
                        self._state = "error"
                        self._detail_code = error.code
                        await self._disconnect_locked()
                    else:
                        return
                delay = min(delay * 2, self._config.reconnect_max_seconds)
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def _request(
        self,
        action: str,
        params: Mapping[str, Any],
        *,
        side_effect_unknown: bool,
    ) -> OneBotActionResponse:
        connection = self._connection
        reader_task = self._reader_task
        if connection is None or reader_task is None or reader_task.done():
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_connection_unavailable",
                "OneBot connection is unavailable",
            )
        if len(self._pending) >= self._config.max_pending_actions:
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_action_queue_full",
                "OneBot action queue is full",
            )

        echo = self._next_echo()
        raw_request = encode_action_request(action, params, echo=echo)
        if len(raw_request.encode("utf-8")) > self._config.max_outbound_frame_bytes:
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_outbound_frame_too_large",
                "OneBot outbound action exceeds the configured frame limit",
            )
        future: asyncio.Future[OneBotActionResponse] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await connection.send(raw_request)
        except Exception as exc:
            self._pending.pop(echo, None)
            error_type = OneBotDeliveryUnknownError if side_effect_unknown else OneBotDriverError
            raise error_type(
                (
                    "onebot_delivery_unknown"
                    if side_effect_unknown
                    else "onebot_action_submit_failed"
                ),
                (
                    "OneBot action submission outcome is unknown"
                    if side_effect_unknown
                    else "OneBot action could not be submitted"
                ),
            ) from exc
        try:
            response = await asyncio.wait_for(
                future,
                timeout=self._config.action_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            error_type = OneBotDeliveryUnknownError if side_effect_unknown else OneBotDriverError
            raise error_type(
                "onebot_delivery_unknown" if side_effect_unknown else "onebot_action_timeout",
                (
                    "OneBot action may have been applied but acknowledgement was not observed"
                    if side_effect_unknown
                    else "OneBot action response timed out"
                ),
            ) from exc
        except Exception as exc:
            if side_effect_unknown:
                raise OneBotDeliveryUnknownError(
                    "onebot_delivery_unknown",
                    "OneBot action may have been applied but acknowledgement was not observed",
                ) from exc
            raise _normalize_driver_error(
                exc,
                default_code="onebot_action_response_failed",
            ) from exc
        finally:
            self._pending.pop(echo, None)
        if not response.ok:
            raise OneBotDefinitelyNotSubmittedError(
                "onebot_action_rejected",
                "OneBot provider rejected the action",
            )
        return response

    async def _verify_login(self, connection: OneBotConnection) -> OneBotActionResponse:
        """Correlate startup identity before the normal event reader can emit ingress."""

        echo = self._next_echo()
        request = encode_action_request("get_login_info", {}, echo=echo)
        try:
            await connection.send(request)
        except Exception as exc:
            raise OneBotDriverError(
                "onebot_identity_submit_failed",
                "OneBot login identity action could not be submitted",
            ) from exc
        deadline = asyncio.get_running_loop().time() + self._config.action_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise OneBotDriverError(
                    "onebot_identity_timeout",
                    "OneBot get_login_info response timed out",
                )
            try:
                raw = await asyncio.wait_for(connection.recv(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise OneBotDriverError(
                    "onebot_identity_timeout",
                    "OneBot get_login_info response timed out",
                ) from exc
            frame = parse_native_frame(raw, max_frame_bytes=self._config.max_frame_bytes)
            response = decode_action_response(frame)
            if response is None:
                # Events preceding identity proof are deliberately not accepted. Frames
                # already queued after the matching response remain for the normal reader.
                continue
            if response.retcode == 1403:
                raise OneBotDriverError(
                    "onebot_authentication_rejected",
                    "OneBot provider rejected the access token",
                )
            if response.echo != echo:
                continue
            if not response.ok:
                raise OneBotDriverError(
                    "onebot_identity_rejected",
                    "OneBot provider rejected get_login_info",
                )
            return response

    async def _reader_loop(self, connection: OneBotConnection) -> None:
        try:
            while True:
                raw = await connection.recv()
                frame = parse_native_frame(
                    raw,
                    max_frame_bytes=self._config.max_frame_bytes,
                )
                response = decode_action_response(frame)
                if response is not None:
                    if response.retcode == 1403:
                        raise OneBotDriverError(
                            "onebot_authentication_rejected",
                            "OneBot provider rejected the access token",
                        )
                    if response.echo is not None:
                        pending = self._pending.get(response.echo)
                        if pending is not None and not pending.done():
                            pending.set_result(response)
                    continue
                if not self._ready:
                    # A provider can emit events during login verification. They are not
                    # authoritative until get_login_info proves the connected account.
                    continue
                generation = self._connection_generation
                if generation is None:
                    raise OneBotDriverError(
                        "onebot_connection_generation_missing",
                        "OneBot connection generation is missing",
                    )
                decoded = decode_inbound_message(
                    frame,
                    account_id=self._config.account_id,
                    connection_generation=generation,
                    observed_at=time.time(),
                    resource_ticket_ttl_seconds=self._config.resource_ticket_ttl_seconds,
                )
                if decoded.event is not None:
                    try:
                        self._event_queue.put_nowait(decoded.event)
                    except asyncio.QueueFull as exc:
                        raise OneBotDriverError(
                            "onebot_ingress_queue_full",
                            "OneBot ingress queue reached its configured limit",
                        ) from exc
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._record_connection_failure(connection, exc)

    async def _event_worker_loop(
        self,
        connection: OneBotConnection,
        queue: asyncio.Queue[CanonicalInboundEvent],
    ) -> None:
        try:
            while self._ready:
                event = await queue.get()
                try:
                    if not self._ready:
                        return
                    try:
                        async with self._event_lane(event):
                            await self._on_event(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Admission, resource, Agent, and durable-run failures belong to
                        # the application event, not to the authenticated WS connection.
                        _LOGGER.warning(
                            "OneBot event processing failed | channel=%s error_type=%s",
                            self._config.channel_id,
                            type(exc).__name__,
                        )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._record_connection_failure(connection, exc)

    def _event_lane(self, event: CanonicalInboundEvent) -> asyncio.Lock:
        evidence = event.evidence
        conversation = evidence.conversation
        digest = hashlib.sha256(
            (
                evidence.account.channel
                + "\0"
                + evidence.account.account_id
                + "\0"
                + conversation.kind
                + "\0"
                + conversation.conversation_id
            ).encode("utf-8")
        ).digest()
        return self._event_lanes[int.from_bytes(digest[:8], "big") % len(self._event_lanes)]

    async def _record_connection_failure(
        self,
        connection: OneBotConnection,
        exc: Exception,
    ) -> None:
        if self._connection is not connection:
            return
        error = _normalize_driver_error(exc, default_code="onebot_connection_failed")
        self._connection = None
        self._ready = False
        self._state = "error"
        self._detail_code = error.code
        self._fail_pending(error)
        try:
            await connection.close()
        except Exception:
            pass
        self._schedule_reconnect()

    async def _disconnect_locked(self) -> None:
        self._ready = False
        reader_task = self._reader_task
        self._reader_task = None
        event_tasks = self._event_tasks
        self._event_tasks = ()
        current_task = asyncio.current_task()
        for task in (reader_task, *event_tasks):
            if task is not None and task is not current_task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass
        self._fail_pending(
            OneBotDriverError(
                "onebot_connection_closed",
                "OneBot connection was closed",
            )
        )

    def _fail_pending(self, error: OneBotDriverError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _next_echo(self) -> str:
        generation = self._connection_generation
        if generation is None:
            raise OneBotDriverError(
                "onebot_connection_generation_missing",
                "OneBot connection generation is missing",
            )
        self._echo_counter += 1
        return f"{generation}-{self._echo_counter}-{secrets.token_hex(8)}"


async def _connect_websocket(config: OneBotChannelConfig) -> OneBotConnection:
    import websockets

    headers = {"Authorization": f"Bearer {config.access_token}"}
    options: dict[str, Any] = {
        "open_timeout": config.action_timeout_seconds,
        "close_timeout": 2,
        "max_size": config.max_frame_bytes,
        "ping_interval": 20,
        "ping_timeout": 20,
    }
    try:
        return await websockets.connect(
            config.websocket_url,
            additional_headers=headers,
            **options,
        )
    except TypeError:
        return await websockets.connect(
            config.websocket_url,
            extra_headers=headers,
            **options,
        )


def _login_account(data: Any) -> str:
    if not isinstance(data, dict):
        raise OneBotDriverError(
            "onebot_login_info_invalid",
            "OneBot get_login_info response has no data object",
        )
    value = data.get("user_id")
    if isinstance(value, bool):
        normalized = ""
    else:
        normalized = str(value or "").strip()
    if not normalized:
        raise OneBotDriverError(
            "onebot_login_info_invalid",
            "OneBot get_login_info response has no account",
        )
    return normalized


def _provider_message_id(data: Any) -> str | None:
    if not isinstance(data, dict) or data.get("message_id") is None:
        return None
    value = data.get("message_id")
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > 256
        or not all(char.isprintable() for char in normalized)
    ):
        return None
    return normalized


def _normalize_driver_error(exc: Exception, *, default_code: str) -> OneBotDriverError:
    if isinstance(exc, OneBotDriverError):
        return exc
    if isinstance(exc, OneBotCodecError):
        return OneBotDriverError(exc.code, str(exc))
    return OneBotDriverError(default_code, f"OneBot driver failed ({type(exc).__name__})")


__all__ = [
    "ConnectionFactory",
    "OneBotConnection",
    "OneBotDefinitelyNotSubmittedError",
    "OneBotDeliveryUnknownError",
    "OneBotDriverError",
    "OneBotForwardWebSocketDriver",
]
