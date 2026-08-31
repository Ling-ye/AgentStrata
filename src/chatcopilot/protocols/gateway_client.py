"""Strict authenticated WebSocket client for the AgentStrata Gateway v1 protocol."""

from __future__ import annotations

import asyncio
import math
import re
from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

from chatcopilot.contracts.gateway_protocol import (
    EventFrame,
    GatewayScope,
    RequestFrame,
    ResponseFrame,
)
from chatcopilot.contracts.gateway_rpc import (
    EventsReplayParams,
    EventsReplayResult,
    GatewayEventPayload,
    GatewayMethodResult,
    GatewayRequestParams,
)
from chatcopilot.gateway.protocol import (
    CONNECT_CHALLENGE_EVENT,
    GATEWAY_EVENTS,
    GATEWAY_METHODS,
    GATEWAY_SCOPES,
    MAX_FRAME_BYTES,
    MUTATION_METHODS,
    PROTOCOL_MAX_VERSION,
    PROTOCOL_MIN_VERSION,
    GatewayProtocolError,
    decode_frame,
    encode_frame,
    events_for_scopes,
    methods_for_scopes,
    validate_request_access,
)
from chatcopilot.gateway.rpc_validation import (
    parse_event_payload,
    parse_method_result,
    serialize_request_params,
)


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_HELLO_LIMIT_KEYS = frozenset({"maxFrameBytes", "maxCollectionItems", "maxStringChars"})


class GatewayClientError(RuntimeError):
    """Stable client failure that never embeds credentials or private wire values."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GatewayConnectionClosed(GatewayClientError):
    pass


class GatewayMutationOutcomeUnknown(GatewayClientError):
    pass


class GatewayRecoveryRequired(GatewayClientError):
    pass


class GatewayRemoteError(GatewayClientError):
    """A redacted Gateway response error with stable public data."""

    def __init__(
        self,
        code: str,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.data = dict(data) if data is not None else None
        super().__init__(code, message)

    def __repr__(self) -> str:
        return f"GatewayRemoteError(code={self.code!r})"


@dataclass(frozen=True)
class GatewayHello:
    protocol: int
    client_id: str
    scopes: tuple[GatewayScope, ...]
    methods: tuple[str, ...]
    events: tuple[str, ...]
    server_generation: int
    event_cursor: int
    policy_version: str
    limits: Mapping[str, int]


@dataclass(frozen=True)
class TypedGatewayEvent:
    event: str
    seq: int
    payload: GatewayEventPayload


@dataclass(frozen=True)
class GatewayClientConfig:
    url: str
    token: str = field(repr=False)
    client_id: str = "acp-edge"
    client_version: str = "1.0.0"
    client_mode: str = "acp"
    scopes: tuple[GatewayScope, ...] = (
        "gateway.read",
        "chat.write",
        "chat.abort",
    )
    capabilities: tuple[str, ...] = ("event-replay",)
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 30.0
    close_timeout_seconds: float = 5.0
    max_pending_requests: int = 128
    max_subscription_queue: int = 128
    max_deferred_events: int = 256
    replay_limit: int = 100
    resume_event_cursor: int | None = None

    def __post_init__(self) -> None:
        _validate_gateway_url(self.url)
        if _TOKEN_RE.fullmatch(self.token) is None:
            raise ValueError("Gateway token must be 32-128 URL-safe characters")
        _validate_identifier(self.client_id, "client_id")
        _validate_identifier(self.client_mode, "client_mode")
        if not self.client_version or len(self.client_version) > 64:
            raise ValueError("client_version is invalid")
        scopes = tuple(self.scopes)
        capabilities = tuple(self.capabilities)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "capabilities", capabilities)
        if not scopes or len(set(scopes)) != len(scopes) or set(scopes).difference(GATEWAY_SCOPES):
            raise ValueError("scopes must contain unique known Gateway scopes")
        if len(set(capabilities)) != len(capabilities) or any(
            _IDENTIFIER_RE.fullmatch(value) is None for value in capabilities
        ):
            raise ValueError("capabilities must contain unique identifiers")
        for value, label in (
            (self.connect_timeout_seconds, "connect_timeout_seconds"),
            (self.request_timeout_seconds, "request_timeout_seconds"),
            (self.close_timeout_seconds, "close_timeout_seconds"),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be positive")
        _bounded_int(self.max_pending_requests, "max_pending_requests", 1, 4096)
        _bounded_int(self.max_subscription_queue, "max_subscription_queue", 1, 4096)
        _bounded_int(self.max_deferred_events, "max_deferred_events", 1, 8192)
        _bounded_int(self.replay_limit, "replay_limit", 1, 256)
        if self.resume_event_cursor is not None and (
            type(self.resume_event_cursor) is not int or self.resume_event_cursor < 0
        ):
            raise ValueError("resume_event_cursor must be a non-negative integer")


class GatewayEventSubscriptionProtocol(Protocol):
    async def get(self) -> TypedGatewayEvent: ...

    def close(self) -> None: ...


class GatewayRpcClientProtocol(Protocol):
    @property
    def connected(self) -> bool: ...

    async def ensure_connected(self) -> None: ...

    async def request(
        self,
        method: str,
        params: GatewayRequestParams,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayMethodResult: ...

    def subscribe(
        self,
        *,
        events: Collection[str],
        session_id: str | None = None,
        max_queue: int | None = None,
    ) -> GatewayEventSubscriptionProtocol: ...


@dataclass
class _PendingRequest:
    method: str
    mutation: bool
    future: asyncio.Future[GatewayMethodResult]


@dataclass(frozen=True)
class _SubscriptionFailure:
    error: GatewayClientError


@dataclass(eq=False)
class GatewayEventSubscription:
    _client: GatewayWebSocketClient
    _events: frozenset[str]
    _session_id: str | None
    _queue: asyncio.Queue[TypedGatewayEvent | _SubscriptionFailure]
    _closed: bool = False

    async def get(self) -> TypedGatewayEvent:
        if self._closed and self._queue.empty():
            raise GatewayConnectionClosed(
                "subscription_closed",
                "Gateway event subscription is closed",
            )
        item = await self._queue.get()
        if isinstance(item, _SubscriptionFailure):
            self._closed = True
            raise item.error
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._fail(
            GatewayConnectionClosed(
                "subscription_closed",
                "Gateway event subscription is closed",
            )
        )

    def _matches(self, event: TypedGatewayEvent) -> bool:
        if event.event not in self._events:
            return False
        if self._session_id is None:
            return True
        payload_session = getattr(event.payload, "session_id", None)
        if payload_session is None and event.event == "session.updated":
            session = getattr(event.payload, "session", None)
            payload_session = getattr(session, "session_id", None)
        return payload_session == self._session_id

    def _deliver(self, event: TypedGatewayEvent) -> None:
        if self._closed or not self._matches(event):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._fail(
                GatewayClientError(
                    "event_backpressure",
                    "Gateway event consumer did not keep up",
                )
            )

    def _fail(self, error: GatewayClientError) -> None:
        if self._closed:
            return
        self._closed = True
        self._client._subscriptions.discard(self)
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._queue.put_nowait(_SubscriptionFailure(error))
        except asyncio.QueueFull:
            pass


class GatewayWebSocketClient:
    """One loop-bound Gateway connection with correlated RPC and bounded events."""

    def __init__(self, config: GatewayClientConfig) -> None:
        self.config = config
        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self._abandoned_ids: deque[str] = deque(maxlen=1024)
        self._abandoned_set: set[str] = set()
        self._subscriptions: set[GatewayEventSubscription] = set()
        self._hello: GatewayHello | None = None
        self._event_cursor = config.resume_event_cursor
        self._deferred_events: list[TypedGatewayEvent] = []
        self._replaying = False
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._websocket is not None and self._reader_task is not None

    @property
    def hello(self) -> GatewayHello | None:
        return self._hello

    @property
    def event_cursor(self) -> int | None:
        return self._event_cursor

    async def connect(self) -> GatewayHello:
        if self.connected:
            raise GatewayClientError("already_connected", "Gateway client is already connected")
        loop = asyncio.get_running_loop()
        if self._owner_loop is not None and self._owner_loop is not loop:
            raise GatewayClientError(
                "event_loop_mismatch",
                "Gateway client cannot move between event loops",
            )
        self._owner_loop = loop
        self._closing = False
        websocket = await self._open_websocket()
        try:
            hello = await self._handshake(websocket)
        except BaseException:
            await _close_websocket(websocket, self.config.close_timeout_seconds)
            raise
        self._websocket = websocket
        self._hello = hello
        replay_from = self._event_cursor
        if replay_from is None:
            self._event_cursor = hello.event_cursor
        self._replaying = replay_from is not None and replay_from != hello.event_cursor
        self._reader_task = asyncio.create_task(self._reader_loop(websocket))
        if self._replaying:
            try:
                await self._replay_events(hello.event_cursor)
                self._replaying = False
                self._flush_deferred_events()
            except BaseException:
                await self._disconnect(
                    GatewayRecoveryRequired(
                        "event_recovery_failed",
                        "Gateway event recovery failed",
                    )
                )
                raise
        return hello

    async def ensure_connected(self) -> None:
        """Restore the authenticated connection without racing another recovery caller."""

        async with self._connect_lock:
            if self.connected:
                return
            await self.connect()

    async def close(self) -> None:
        self._closing = True
        error = GatewayConnectionClosed("client_closed", "Gateway client closed")
        websocket = self._websocket
        reader = self._reader_task
        self._websocket = None
        self._reader_task = None
        if websocket is not None:
            await _close_websocket(websocket, self.config.close_timeout_seconds)
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self._fail_pending(error)
        self._fail_subscriptions(error)
        self._hello = None
        self._replaying = False
        self._deferred_events.clear()
        self._closing = False

    async def request(
        self,
        method: str,
        params: GatewayRequestParams,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayMethodResult:
        websocket = self._websocket
        hello = self._hello
        if websocket is None or hello is None or self._reader_task is None:
            raise GatewayConnectionClosed(
                "gateway_not_connected",
                "Gateway client is not connected",
            )
        active_websocket: Any = websocket
        if method not in GATEWAY_METHODS:
            raise GatewayClientError("unknown_method", "Gateway method is not recognized")
        if len(self._pending) >= self.config.max_pending_requests:
            raise GatewayClientError(
                "request_backpressure",
                "Gateway client request capacity is reached",
            )
        request_id = uuid4().hex
        try:
            frame = RequestFrame(
                request_id=request_id,
                method=method,
                params=serialize_request_params(method, params),
                idempotency_key=idempotency_key,
            )
            validate_request_access(frame, scopes=hello.scopes)
            encoded = encode_frame(frame, max_bytes=_hello_frame_limit(hello)).decode("utf-8")
        except GatewayProtocolError as exc:
            raise GatewayClientError(
                exc.code,
                "Gateway request is invalid",
            ) from exc
        future: asyncio.Future[GatewayMethodResult] = asyncio.get_running_loop().create_future()
        pending = _PendingRequest(method=method, mutation=method in MUTATION_METHODS, future=future)
        self._pending[request_id] = pending
        try:
            async with self._send_lock:
                if websocket is not self._websocket:
                    raise GatewayConnectionClosed(
                        "gateway_connection_changed",
                        "Gateway connection changed before request submission",
                    )
                await active_websocket.send(encoded)
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.config.request_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._abandon(request_id)
            if pending.mutation:
                raise GatewayMutationOutcomeUnknown(
                    "mutation_outcome_unknown",
                    "Gateway mutation completion is unknown",
                ) from exc
            raise GatewayClientError(
                "request_timeout",
                "Gateway request timed out",
            ) from exc
        except asyncio.CancelledError:
            self._abandon(request_id)
            raise
        except GatewayClientError:
            if not future.done():
                self._abandon(request_id)
            raise
        except Exception as exc:
            self._abandon(request_id)
            if pending.mutation:
                raise GatewayMutationOutcomeUnknown(
                    "mutation_outcome_unknown",
                    "Gateway mutation completion is unknown",
                ) from exc
            raise GatewayConnectionClosed(
                "gateway_connection_lost",
                "Gateway connection was lost",
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def subscribe(
        self,
        *,
        events: Collection[str],
        session_id: str | None = None,
        max_queue: int | None = None,
    ) -> GatewayEventSubscription:
        if not self.connected:
            raise GatewayConnectionClosed(
                "gateway_not_connected",
                "Gateway client is not connected",
            )
        selected = frozenset(events)
        if not selected or selected.difference(GATEWAY_EVENTS):
            raise ValueError("events must contain known Gateway events")
        if session_id is not None:
            _validate_identifier(session_id, "session_id")
        queue_size = self.config.max_subscription_queue if max_queue is None else max_queue
        _bounded_int(queue_size, "max_queue", 1, 4096)
        subscription = GatewayEventSubscription(
            _client=self,
            _events=selected,
            _session_id=session_id,
            _queue=asyncio.Queue(maxsize=queue_size),
        )
        self._subscriptions.add(subscription)
        return subscription

    async def _open_websocket(self) -> Any:
        try:
            import websockets
        except ImportError as exc:
            raise GatewayClientError(
                "websocket_dependency_missing",
                "the websockets dependency is required for the Gateway client",
            ) from exc
        options: dict[str, Any] = {
            "compression": None,
            "open_timeout": self.config.connect_timeout_seconds,
            "close_timeout": self.config.close_timeout_seconds,
            "max_size": MAX_FRAME_BYTES,
            "max_queue": 16,
        }
        try:
            try:
                return await websockets.connect(self.config.url, proxy=None, **options)
            except TypeError:
                return await websockets.connect(self.config.url, **options)
        except Exception as exc:
            raise GatewayConnectionClosed(
                "gateway_connect_failed",
                "Gateway loopback connection failed",
            ) from exc

    async def _handshake(self, websocket: Any) -> GatewayHello:
        try:
            raw_challenge = await asyncio.wait_for(
                websocket.recv(),
                timeout=self.config.connect_timeout_seconds,
            )
            challenge = decode_frame(raw_challenge)
            nonce = _parse_challenge(challenge)
            request_id = uuid4().hex
            connect_frame = RequestFrame(
                request_id=request_id,
                method="connect",
                params={
                    "nonce": nonce,
                    "minProtocol": PROTOCOL_MIN_VERSION,
                    "maxProtocol": PROTOCOL_MAX_VERSION,
                    "client": {
                        "id": self.config.client_id,
                        "version": self.config.client_version,
                        "mode": self.config.client_mode,
                    },
                    "scopes": list(self.config.scopes),
                    "capabilities": list(self.config.capabilities),
                    "auth": {"token": self.config.token},
                },
            )
            await websocket.send(encode_frame(connect_frame).decode("utf-8"))
            raw_response = await asyncio.wait_for(
                websocket.recv(),
                timeout=self.config.connect_timeout_seconds,
            )
            response = decode_frame(raw_response)
        except asyncio.TimeoutError as exc:
            raise GatewayConnectionClosed(
                "gateway_handshake_timeout",
                "Gateway handshake timed out",
            ) from exc
        except GatewayClientError:
            raise
        except Exception as exc:
            raise GatewayConnectionClosed(
                "gateway_handshake_failed",
                "Gateway handshake failed",
            ) from exc
        if not isinstance(response, ResponseFrame) or response.request_id != request_id:
            raise GatewayClientError(
                "invalid_hello",
                "Gateway handshake response is invalid",
            )
        if not response.ok:
            error = response.error
            raise GatewayRemoteError(
                error.code if error is not None else "authentication_failed",
                error.message if error is not None else "Gateway authentication failed",
                error.data if error is not None else None,
            )
        return _parse_hello(response.result or {}, self.config)

    async def _reader_loop(self, websocket: Any) -> None:
        failure: GatewayClientError = GatewayConnectionClosed(
            "gateway_connection_lost",
            "Gateway connection was lost",
        )
        try:
            async for raw in websocket:
                frame = decode_frame(raw, max_bytes=_hello_frame_limit(self._hello))
                if isinstance(frame, ResponseFrame):
                    self._handle_response(frame)
                elif isinstance(frame, EventFrame):
                    self._handle_event(frame)
                else:
                    raise GatewayProtocolError(
                        "invalid_frame",
                        "Gateway sent a request frame to a client",
                    )
        except asyncio.CancelledError:
            if not self._closing:
                failure = GatewayConnectionClosed(
                    "gateway_reader_cancelled",
                    "Gateway event reader stopped",
                )
            raise
        except GatewayRecoveryRequired as exc:
            failure = exc
        except GatewayProtocolError:
            failure = GatewayClientError(
                "gateway_protocol_violation",
                "Gateway sent an invalid protocol frame",
            )
        except Exception:
            pass
        finally:
            if websocket is self._websocket:
                self._websocket = None
                self._reader_task = None
                self._hello = None
                self._fail_pending(failure)
                self._fail_subscriptions(failure)

    def _handle_response(self, frame: ResponseFrame) -> None:
        pending = self._pending.get(frame.request_id)
        if pending is None:
            if frame.request_id in self._abandoned_set:
                self._discard_abandoned(frame.request_id)
                return
            raise GatewayProtocolError(
                "unexpected_response",
                "Gateway response does not match a pending request",
            )
        if pending.future.done():
            raise GatewayProtocolError(
                "duplicate_response",
                "Gateway response was delivered more than once",
            )
        if frame.ok:
            try:
                result = parse_method_result(pending.method, frame.result or {})
            except GatewayProtocolError as exc:
                pending.future.set_exception(
                    GatewayClientError(
                        "invalid_rpc_result",
                        "Gateway method result is invalid",
                    )
                )
                raise exc
            pending.future.set_result(result)
            return
        error = frame.error
        if error is None:
            raise GatewayProtocolError("invalid_frame", "Gateway error response is empty")
        if pending.mutation and error.code in {
            "idempotency_recovery_required",
            "mutation_outcome_unknown",
            "mutation_reconciliation_failed",
            "request_in_progress",
            "request_timeout",
        }:
            pending.future.set_exception(
                GatewayMutationOutcomeUnknown(
                    "mutation_outcome_unknown",
                    "Gateway mutation completion is unknown",
                )
            )
            return
        pending.future.set_exception(GatewayRemoteError(error.code, error.message, error.data))

    def _handle_event(self, frame: EventFrame) -> None:
        if frame.event not in events_for_scopes(self.config.scopes):
            raise GatewayProtocolError(
                "scope_denied",
                "Gateway delivered an event outside client scopes",
            )
        typed = TypedGatewayEvent(
            event=frame.event,
            seq=frame.seq,
            payload=parse_event_payload(frame.event, frame.payload),
        )
        if self._replaying:
            if len(self._deferred_events) >= self.config.max_deferred_events:
                raise GatewayProtocolError(
                    "event_backpressure",
                    "too many live events arrived during replay",
                )
            self._deferred_events.append(typed)
            return
        self._publish_event(typed)

    async def _replay_events(self, hello_cursor: int) -> None:
        cursor = cast(int, self._event_cursor)
        target = hello_cursor
        while cursor != target:
            result = await self.request(
                "events.replay",
                EventsReplayParams(after_seq=cursor, limit=self.config.replay_limit),
            )
            if not isinstance(result, EventsReplayResult):
                raise GatewayClientError(
                    "invalid_replay_result",
                    "Gateway event replay result is invalid",
                )
            if result.resync_required:
                raise GatewayRecoveryRequired(
                    "event_resync_required",
                    "Gateway event history can no longer be resumed",
                )
            for item in result.events:
                self._publish_event(
                    TypedGatewayEvent(item.event, item.seq, item.payload),
                    advance_cursor=False,
                )
            if result.next_cursor <= cursor and result.current_cursor > cursor:
                raise GatewayClientError(
                    "event_replay_stalled",
                    "Gateway event replay did not advance",
                )
            cursor = result.next_cursor
            self._event_cursor = cursor
            target = result.current_cursor

    def _flush_deferred_events(self) -> None:
        deferred = sorted(self._deferred_events, key=lambda item: item.seq)
        self._deferred_events.clear()
        observed: dict[int, TypedGatewayEvent] = {}
        for event in deferred:
            previous = observed.get(event.seq)
            if previous is not None and previous != event:
                raise GatewayRecoveryRequired(
                    "event_sequence_conflict",
                    "Gateway event sequence changed during recovery",
                )
            observed[event.seq] = event
        for event in observed.values():
            self._publish_event(event)

    def _publish_event(self, event: TypedGatewayEvent, *, advance_cursor: bool = True) -> None:
        cursor = self._event_cursor or 0
        if event.seq <= cursor:
            return
        for subscription in tuple(self._subscriptions):
            subscription._deliver(event)
        if advance_cursor:
            self._event_cursor = event.seq

    async def _disconnect(self, error: GatewayClientError) -> None:
        websocket = self._websocket
        reader = self._reader_task
        self._websocket = None
        self._reader_task = None
        self._hello = None
        if websocket is not None:
            await _close_websocket(websocket, self.config.close_timeout_seconds)
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self._fail_pending(error)
        self._fail_subscriptions(error)
        self._replaying = False
        self._deferred_events.clear()

    def _fail_pending(self, error: GatewayClientError) -> None:
        for pending in tuple(self._pending.values()):
            if pending.future.done():
                continue
            failure: GatewayClientError = error
            if pending.mutation:
                failure = GatewayMutationOutcomeUnknown(
                    "mutation_outcome_unknown",
                    "Gateway mutation completion is unknown",
                )
            pending.future.set_exception(failure)

    def _fail_subscriptions(self, error: GatewayClientError) -> None:
        for subscription in tuple(self._subscriptions):
            subscription._fail(error)

    def _abandon(self, request_id: str) -> None:
        if request_id in self._abandoned_set:
            return
        if len(self._abandoned_ids) == self._abandoned_ids.maxlen:
            oldest = self._abandoned_ids.popleft()
            self._abandoned_set.discard(oldest)
        self._abandoned_ids.append(request_id)
        self._abandoned_set.add(request_id)

    def _discard_abandoned(self, request_id: str) -> None:
        self._abandoned_set.discard(request_id)


def _parse_challenge(frame: object) -> str:
    if not isinstance(frame, EventFrame) or frame.event != CONNECT_CHALLENGE_EVENT:
        raise GatewayClientError(
            "connect_challenge_required",
            "Gateway did not send a connect challenge",
        )
    payload = frame.payload
    if set(payload) != {
        "nonce",
        "issuedAt",
        "expiresAt",
        "minProtocol",
        "maxProtocol",
    }:
        raise GatewayClientError("invalid_challenge", "Gateway connect challenge is invalid")
    nonce = payload.get("nonce")
    issued = payload.get("issuedAt")
    expires = payload.get("expiresAt")
    minimum = payload.get("minProtocol")
    maximum = payload.get("maxProtocol")
    if (
        not isinstance(nonce, str)
        or _TOKEN_RE.fullmatch(nonce) is None
        or type(issued) is not int
        or type(expires) is not int
        or issued < 0
        or expires <= issued
        or type(minimum) is not int
        or type(maximum) is not int
        or maximum < PROTOCOL_MIN_VERSION
        or minimum > PROTOCOL_MAX_VERSION
    ):
        raise GatewayClientError("invalid_challenge", "Gateway connect challenge is invalid")
    return nonce


def _parse_hello(result: Mapping[str, Any], config: GatewayClientConfig) -> GatewayHello:
    expected_keys = {
        "type",
        "protocol",
        "clientId",
        "scopes",
        "methods",
        "events",
        "serverGeneration",
        "eventCursor",
        "policyVersion",
        "limits",
    }
    if set(result) != expected_keys or result.get("type") != "hello-ok":
        raise GatewayClientError("invalid_hello", "Gateway hello response is invalid")
    scopes = _string_tuple(result.get("scopes"), "scopes")
    methods = _string_tuple(result.get("methods"), "methods")
    events = _string_tuple(result.get("events"), "events")
    protocol = result.get("protocol")
    generation = result.get("serverGeneration")
    cursor = result.get("eventCursor")
    client_id = result.get("clientId")
    policy_version = result.get("policyVersion")
    limits_value = result.get("limits")
    if (
        protocol != PROTOCOL_MAX_VERSION
        or client_id != config.client_id
        or scopes != config.scopes
        or methods != methods_for_scopes(config.scopes)
        or events != events_for_scopes(config.scopes)
        or type(generation) is not int
        or generation < 1
        or type(cursor) is not int
        or cursor < 0
        or not isinstance(policy_version, str)
        or not policy_version
        or len(policy_version) > 128
        or not isinstance(limits_value, Mapping)
        or set(limits_value) != _HELLO_LIMIT_KEYS
        or any(type(value) is not int or value < 1 for value in limits_value.values())
    ):
        raise GatewayClientError("invalid_hello", "Gateway hello response is invalid")
    return GatewayHello(
        protocol=protocol,
        client_id=client_id,
        scopes=cast(tuple[GatewayScope, ...], scopes),
        methods=methods,
        events=events,
        server_generation=generation,
        event_cursor=cursor,
        policy_version=policy_version,
        limits=cast(Mapping[str, int], dict(limits_value)),
    )


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 64
        or any(
            not isinstance(item, str) or _IDENTIFIER_RE.fullmatch(item) is None for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise GatewayClientError("invalid_hello", f"Gateway hello {label} is invalid")
    return tuple(value)


def _hello_frame_limit(hello: GatewayHello | None) -> int:
    if hello is None:
        return MAX_FRAME_BYTES
    configured = hello.limits.get("maxFrameBytes", MAX_FRAME_BYTES)
    return min(configured, MAX_FRAME_BYTES)


def _validate_gateway_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Gateway URL is invalid") from exc
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or port < 1
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Gateway URL must be an exact loopback WebSocket endpoint")


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _bounded_int(value: int, label: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")


async def _close_websocket(websocket: Any, timeout: float) -> None:
    try:
        await asyncio.wait_for(websocket.close(code=1000, reason="client_closing"), timeout=timeout)
    except Exception:
        pass


__all__ = [
    "GatewayClientConfig",
    "GatewayClientError",
    "GatewayConnectionClosed",
    "GatewayEventSubscription",
    "GatewayEventSubscriptionProtocol",
    "GatewayHello",
    "GatewayMutationOutcomeUnknown",
    "GatewayRecoveryRequired",
    "GatewayRemoteError",
    "GatewayRpcClientProtocol",
    "GatewayWebSocketClient",
    "TypedGatewayEvent",
]
