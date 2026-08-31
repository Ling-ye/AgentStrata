"""Authenticated loopback WebSocket host for the AgentStrata Gateway v1 protocol."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from chatcopilot.contracts.gateway_protocol import (
    EventFrame,
    GatewayScope,
    RequestFrame,
    ResponseError,
    ResponseFrame,
)

from .protocol import (
    GATEWAY_EVENTS,
    GATEWAY_SCOPES,
    MAX_FRAME_BYTES,
    MUTATION_METHODS,
    GatewayCredentialAuthority,
    GatewayHandshakeAuthority,
    GatewayProtocolError,
    decode_frame,
    encode_frame,
    events_for_scopes,
    parse_connect_request,
    request_fingerprint,
    validate_request_access,
)
from .state_store import (
    GatewayStateError,
    GatewayStateStore,
    IdempotencyConflict,
    StaleWriterGeneration,
)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_PROTOCOL_ERROR_MESSAGES: Mapping[str, str] = {
    "already_connected": "connect is only valid as the first request",
    "authentication_failed": "Gateway authentication failed",
    "challenge_expired": "Gateway challenge expired",
    "connect_required": "the first client request must be connect",
    "frame_too_large": "Gateway frame exceeds the configured limit",
    "idempotency_key_required": "Gateway mutation requires an idempotency key",
    "invalid_challenge": "Gateway challenge is invalid or already consumed",
    "invalid_connect": "Gateway connect request is invalid",
    "invalid_frame": "Gateway frame is invalid",
    "invalid_json": "Gateway frame is not valid JSON",
    "protocol_version_mismatch": "Gateway protocol versions do not overlap",
    "scope_denied": "Gateway scope does not allow this request",
    "unknown_method": "Gateway method is not recognized",
}


class GatewayServerLifecycleError(RuntimeError):
    """Raised when the WebSocket host cannot safely enter the requested lifecycle state."""


class GatewayDispatchError(RuntimeError):
    """A dispatcher-owned stable error that is safe to return to the authenticated client."""

    def __init__(
        self,
        code: str,
        public_message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if not _ERROR_CODE_RE.fullmatch(code):
            raise ValueError("dispatch error code is invalid")
        if not public_message or len(public_message) > 512:
            raise ValueError("dispatch public message is invalid")
        self.code = code
        self.public_message = public_message
        self.data = dict(data) if data is not None else None
        super().__init__(code)


@dataclass(frozen=True)
class GatewayClientContext:
    """Authenticated transport identity projected to the request dispatcher."""

    client_id: str
    client_version: str
    client_mode: str
    protocol: int
    scopes: tuple[GatewayScope, ...]
    capabilities: tuple[str, ...]


class GatewayRequestDispatcher(Protocol):
    """Application-owned request boundary; the transport host knows no domain implementation.

    A dispatcher must detach a durable domain run before returning an accepted result.
    Cancellation of this coroutine is transport cleanup and must not be translated into
    a domain ``chat.abort`` without a separate authenticated request.
    """

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class GatewayMutationReconciliation:
    """Method-owned proof that a reserved mutation did or did not enter its domain."""

    state: Literal["domain_started", "domain_not_started"]
    result: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state == "domain_started" and self.result is None:
            raise ValueError("started mutation reconciliation requires a result")
        if self.state == "domain_not_started" and self.result is not None:
            raise ValueError("unstarted mutation reconciliation cannot include a result")


class GatewayEventVisibilityPolicy(Protocol):
    """Application-owned per-client event visibility decision."""

    def can_view(
        self,
        *,
        client: GatewayClientContext,
        event: EventFrame,
    ) -> bool: ...


@dataclass(frozen=True)
class GatewayServerConfig:
    host: str = "127.0.0.1"
    port: int = 0
    policy_version: str = "1"
    handshake_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 30.0
    close_timeout_seconds: float = 5.0
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_connections: int = 64
    max_inbound_queue: int = 16
    max_event_queue: int = 128
    max_inflight_requests_per_connection: int = 16
    max_total_inflight_requests: int = 256
    replay_limit: int = 100

    def __post_init__(self) -> None:
        if self.host not in _LOOPBACK_HOSTS:
            raise ValueError("Gateway host must be exactly 127.0.0.1 or ::1")
        if type(self.port) is not int or self.port < 0 or self.port > 65535:
            raise ValueError("Gateway port must be between 0 and 65535")
        if (
            not isinstance(self.policy_version, str)
            or not self.policy_version
            or len(self.policy_version) > 128
        ):
            raise ValueError("policy_version is invalid")
        _positive_finite(self.handshake_timeout_seconds, "handshake_timeout_seconds")
        _positive_finite(self.request_timeout_seconds, "request_timeout_seconds")
        _positive_finite(self.close_timeout_seconds, "close_timeout_seconds")
        _bounded_int(self.max_frame_bytes, "max_frame_bytes", minimum=1024, maximum=MAX_FRAME_BYTES)
        _bounded_int(self.max_connections, "max_connections", minimum=1, maximum=1024)
        _bounded_int(self.max_inbound_queue, "max_inbound_queue", minimum=1, maximum=1024)
        _bounded_int(self.max_event_queue, "max_event_queue", minimum=1, maximum=4096)
        _bounded_int(
            self.max_inflight_requests_per_connection,
            "max_inflight_requests_per_connection",
            minimum=1,
            maximum=1024,
        )
        _bounded_int(
            self.max_total_inflight_requests,
            "max_total_inflight_requests",
            minimum=1,
            maximum=16_384,
        )
        if self.max_total_inflight_requests < self.max_inflight_requests_per_connection:
            raise ValueError(
                "max_total_inflight_requests cannot be smaller than the per-connection limit"
            )
        _bounded_int(self.replay_limit, "replay_limit", minimum=1, maximum=1000)


@dataclass(frozen=True)
class GatewayServerHealth:
    running: bool
    accepting: bool
    ready: bool
    host: str
    port: int | None
    server_generation: int
    current_generation: int | None
    active_connections: int
    inflight_requests: int
    event_cursor: int | None


@dataclass(frozen=True)
class GatewayPublishResult:
    queued_connections: int
    closed_connections: int


@dataclass(frozen=True)
class GatewayReplayBatch:
    events: tuple[EventFrame, ...]
    next_cursor: int
    current_cursor: int
    resync_required: bool


@dataclass(eq=False)
class _ConnectionState:
    websocket: Any
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    event_queue: asyncio.Queue[EventFrame] = field(default_factory=asyncio.Queue)
    client: GatewayClientContext | None = None
    event_task: asyncio.Task[None] | None = None
    inflight_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    inflight_request_ids: set[str] = field(default_factory=set)
    closing: bool = False


@dataclass
class _MutationGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class GatewayWebSocketServer:
    """Own one authenticated loopback listener and bounded transport tasks."""

    def __init__(
        self,
        *,
        config: GatewayServerConfig,
        dispatcher: GatewayRequestDispatcher,
        credential_authority: GatewayCredentialAuthority,
        event_visibility_policy: GatewayEventVisibilityPolicy,
        state_store: GatewayStateStore,
        server_generation: int,
    ) -> None:
        if type(server_generation) is not int or server_generation < 1:
            raise ValueError("server_generation must be positive")
        self.config = config
        self.dispatcher = dispatcher
        self.credential_authority = credential_authority
        self.event_visibility_policy = event_visibility_policy
        self.state_store = state_store
        self.server_generation = server_generation
        self._listener: Any | None = None
        self._bound_port: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._connections: set[_ConnectionState] = set()
        self._maintenance_tasks: set[asyncio.Task[None]] = set()
        self._mutation_gates: dict[tuple[str, str, str], _MutationGate] = {}
        self._total_inflight_requests = 0

    @property
    def url(self) -> str:
        if self._bound_port is None:
            raise GatewayServerLifecycleError("Gateway listener is not running")
        host = f"[{self.config.host}]" if self.config.host == "::1" else self.config.host
        return f"ws://{host}:{self._bound_port}"

    async def start(self) -> None:
        if self._listener is not None:
            raise GatewayServerLifecycleError("Gateway listener is already running")
        self._require_current_generation()
        try:
            import websockets
        except ImportError as exc:
            raise GatewayServerLifecycleError(
                "the websockets dependency is required to start the Gateway"
            ) from exc

        self._stopping = False
        self._owner_loop = asyncio.get_running_loop()

        async def handler(websocket: Any, *_: Any) -> None:
            await self._handle_connection(websocket)

        try:
            listener = await websockets.serve(
                handler,
                self.config.host,
                self.config.port,
                compression=None,
                close_timeout=self.config.close_timeout_seconds,
                max_size=self.config.max_frame_bytes,
                max_queue=self.config.max_inbound_queue,
            )
        except asyncio.CancelledError:
            self._owner_loop = None
            raise
        except Exception as exc:  # noqa: BLE001 - lifecycle API exposes one stable bind failure.
            self._owner_loop = None
            raise GatewayServerLifecycleError("Gateway loopback listener could not start") from exc
        sockets = tuple(listener.sockets or ())
        if not sockets:
            listener.close()
            await listener.wait_closed()
            self._owner_loop = None
            raise GatewayServerLifecycleError("Gateway listener did not expose a bound socket")
        self._listener = listener
        self._bound_port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        self._require_owner_loop()
        self._stopping = True
        self._listener = None
        listener.close()
        contexts = tuple(self._connections)
        await asyncio.gather(
            *(self._safe_close(context, 1001, "gateway_stopping") for context in contexts),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(self._shutdown_connection(context) for context in contexts),
            return_exceptions=True,
        )
        await listener.wait_closed()
        maintenance = tuple(self._maintenance_tasks)
        for task in maintenance:
            task.cancel()
        if maintenance:
            await asyncio.gather(*maintenance, return_exceptions=True)
        self._bound_port = None
        self._owner_loop = None
        self._stopping = False

    def health(self) -> GatewayServerHealth:
        current_generation: int | None = None
        event_cursor: int | None = None
        state_ready = False
        try:
            current_generation = self.state_store.current_writer_generation()
            event_cursor = self.state_store.replay_events(0, limit=1).current_cursor
            state_ready = current_generation == self.server_generation
        except Exception:  # noqa: BLE001 - health is a bounded projection, not a traceback surface.
            pass
        running = self._listener is not None
        accepting = running and not self._stopping
        return GatewayServerHealth(
            running=running,
            accepting=accepting,
            ready=accepting and state_ready,
            host=self.config.host,
            port=self._bound_port,
            server_generation=self.server_generation,
            current_generation=current_generation,
            active_connections=len(self._connections),
            inflight_requests=self._total_inflight_requests,
            event_cursor=event_cursor,
        )

    def publish(self, frame: EventFrame) -> GatewayPublishResult:
        """Queue one already-durable event without letting a slow client block its peers."""

        self._require_owner_loop()
        if self._listener is None or self._stopping:
            raise GatewayServerLifecycleError("Gateway listener is not accepting events")
        if frame.event not in GATEWAY_EVENTS or frame.seq < 1:
            raise GatewayProtocolError(
                "invalid_event", "only durable Gateway v1 events can publish"
            )
        encode_frame(frame, max_bytes=self.config.max_frame_bytes)
        self._require_current_generation()
        persisted = self.state_store.events_after(frame.seq - 1, limit=1)
        if (
            not persisted
            or persisted[0].seq != frame.seq
            or persisted[0].event != frame.event
            or dict(persisted[0].payload) != dict(frame.payload)
        ):
            raise GatewayStateError("published event does not match durable Gateway state")

        queued = 0
        closed = 0
        for context in tuple(self._connections):
            client = context.client
            if client is None or context.closing:
                continue
            if not self._can_view_event(client, frame):
                continue
            try:
                context.event_queue.put_nowait(frame)
            except asyncio.QueueFull:
                context.closing = True
                closed += 1
                self._schedule_maintenance(self._safe_close(context, 1013, "event_backpressure"))
            else:
                queued += 1
        return GatewayPublishResult(queued_connections=queued, closed_connections=closed)

    def replay_events(
        self,
        *,
        after_seq: int,
        client: GatewayClientContext,
        limit: int | None = None,
    ) -> GatewayReplayBatch:
        """Read one bounded durable page while projecting only events in client scopes."""

        requested_limit = self.config.replay_limit if limit is None else limit
        if type(requested_limit) is not int or requested_limit < 1:
            raise ValueError("replay limit must be positive")
        if requested_limit > self.config.replay_limit:
            raise ValueError("replay limit exceeds the configured server limit")
        if (
            not client.scopes
            or len(set(client.scopes)) != len(client.scopes)
            or set(client.scopes).difference(GATEWAY_SCOPES)
        ):
            raise ValueError("client scopes are invalid")
        self._require_current_generation()
        replay = self.state_store.replay_events(after_seq, limit=requested_limit)
        allowed_events = frozenset(events_for_scopes(client.scopes))
        frames = tuple(
            EventFrame(event=record.event, seq=record.seq, payload=record.payload)
            for record in replay.events
            if record.event in allowed_events
            and self._can_view_event(
                client,
                EventFrame(event=record.event, seq=record.seq, payload=record.payload),
            )
        )
        next_cursor = replay.events[-1].seq if replay.events else after_seq
        return GatewayReplayBatch(
            events=frames,
            next_cursor=next_cursor,
            current_cursor=replay.current_cursor,
            resync_required=replay.resync_required,
        )

    async def _handle_connection(self, websocket: Any) -> None:
        if self._stopping or len(self._connections) >= self.config.max_connections:
            await _close_websocket(websocket, 1013, "connection_capacity")
            return
        context = _ConnectionState(
            websocket=websocket,
            event_queue=asyncio.Queue(maxsize=self.config.max_event_queue),
        )
        self._connections.add(context)
        try:
            authenticated = await self._authenticate(context)
            if not authenticated:
                return
            context.event_task = asyncio.create_task(self._send_events(context))
            await self._read_requests(context)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - connection errors never become public exception text.
            await self._safe_close(context, 1011, "gateway_connection_error")
        finally:
            await self._shutdown_connection(context)
            self._connections.discard(context)

    async def _authenticate(self, context: _ConnectionState) -> bool:
        try:
            self._require_current_generation()
            authority = GatewayHandshakeAuthority(
                credential_authority=self.credential_authority,
                server_generation=self.server_generation,
                event_cursor=self._current_event_cursor(),
                policy_version=self.config.policy_version,
                challenge_ttl_seconds=self.config.handshake_timeout_seconds,
            )
            challenge = authority.issue_challenge()
            await self._send_frame(context, authority.challenge_event(challenge))
            raw = await asyncio.wait_for(
                context.websocket.recv(),
                timeout=self.config.handshake_timeout_seconds,
            )
            frame = decode_frame(raw, max_bytes=self.config.max_frame_bytes)
        except asyncio.TimeoutError:
            await self._safe_close(context, 1008, "handshake_timeout")
            return False
        except GatewayProtocolError:
            await self._safe_close(context, 1008, "invalid_handshake")
            return False
        except (GatewayServerLifecycleError, GatewayStateError, StaleWriterGeneration):
            await self._safe_close(context, 1012, "gateway_generation_stale")
            return False

        if not isinstance(frame, RequestFrame):
            await self._safe_close(context, 1008, "connect_required")
            return False
        try:
            hello = authority.accept(frame)
        except GatewayProtocolError as exc:
            await self._send_frame(context, _protocol_error_response(frame.request_id, exc.code))
            await self._safe_close(context, 1008, exc.code)
            return False
        connect = parse_connect_request(frame)
        context.client = GatewayClientContext(
            client_id=hello.client_id,
            client_version=connect.client_version,
            client_mode=connect.client_mode,
            protocol=hello.protocol,
            scopes=hello.scopes,
            capabilities=connect.capabilities,
        )
        await self._send_frame(context, authority.hello_response(frame.request_id, hello))
        return True

    async def _read_requests(self, context: _ConnectionState) -> None:
        async for raw in context.websocket:
            try:
                frame = decode_frame(raw, max_bytes=self.config.max_frame_bytes)
            except GatewayProtocolError:
                await self._safe_close(context, 1008, "invalid_frame")
                return
            if not isinstance(frame, RequestFrame):
                await self._safe_close(context, 1008, "request_frame_required")
                return
            client = context.client
            if client is None:
                await self._safe_close(context, 1008, "connect_required")
                return
            try:
                validate_request_access(frame, scopes=client.scopes)
            except GatewayProtocolError as exc:
                await self._send_frame(
                    context,
                    _protocol_error_response(frame.request_id, exc.code),
                )
                continue
            if not self._is_current_generation():
                await self._send_frame(
                    context,
                    _error_response(
                        frame.request_id,
                        "gateway_generation_stale",
                        "Gateway writer generation is no longer current",
                    ),
                )
                await self._safe_close(context, 1012, "gateway_generation_stale")
                return
            if frame.request_id in context.inflight_request_ids:
                await self._send_frame(
                    context,
                    _error_response(
                        frame.request_id,
                        "duplicate_request_id",
                        "request id is already in flight on this connection",
                    ),
                )
                continue
            if len(context.inflight_tasks) >= self.config.max_inflight_requests_per_connection:
                await self._send_frame(
                    context,
                    _error_response(
                        frame.request_id,
                        "too_many_requests",
                        "connection request limit is reached",
                    ),
                )
                continue
            if self._total_inflight_requests >= self.config.max_total_inflight_requests:
                await self._send_frame(
                    context,
                    _error_response(
                        frame.request_id,
                        "gateway_busy",
                        "Gateway request capacity is reached",
                    ),
                )
                continue
            self._start_request(context, frame, client)

    def _start_request(
        self,
        context: _ConnectionState,
        frame: RequestFrame,
        client: GatewayClientContext,
    ) -> None:
        context.inflight_request_ids.add(frame.request_id)
        self._total_inflight_requests += 1
        task = asyncio.create_task(self._serve_request(context, frame, client))
        context.inflight_tasks.add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            context.inflight_tasks.discard(completed)
            context.inflight_request_ids.discard(frame.request_id)
            self._total_inflight_requests = max(0, self._total_inflight_requests - 1)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    async def _serve_request(
        self,
        context: _ConnectionState,
        frame: RequestFrame,
        client: GatewayClientContext,
    ) -> None:
        try:
            response = await self._execute_request(frame, client)
            await self._send_frame(context, response)
        except asyncio.CancelledError:
            # Transport cancellation does not imply that a domain run was aborted.
            raise
        except Exception:  # noqa: BLE001 - never serialize transport or state exception text.
            await self._safe_close(context, 1011, "gateway_request_error")

    async def _execute_request(
        self,
        frame: RequestFrame,
        client: GatewayClientContext,
    ) -> ResponseFrame:
        if frame.method not in MUTATION_METHODS:
            return await self._dispatch(frame, client)
        if frame.idempotency_key is None:
            return _error_response(
                frame.request_id,
                "idempotency_key_required",
                "Gateway mutation requires an idempotency key",
            )
        identity = (client.client_id, frame.method, frame.idempotency_key)
        gate = self._mutation_gates.setdefault(identity, _MutationGate())
        gate.users += 1
        try:
            async with gate.lock:
                return await self._execute_mutation_locked(frame, client)
        finally:
            gate.users -= 1
            if gate.users == 0 and self._mutation_gates.get(identity) is gate:
                self._mutation_gates.pop(identity, None)

    async def _execute_mutation_locked(
        self,
        frame: RequestFrame,
        client: GatewayClientContext,
    ) -> ResponseFrame:
        if frame.idempotency_key is None:
            raise AssertionError("validated mutation is missing its idempotency key")
        fingerprint = request_fingerprint(frame)
        try:
            reservation = self.state_store.reserve_idempotency(
                generation=self.server_generation,
                client_id=client.client_id,
                method=frame.method,
                key=frame.idempotency_key,
                request_fingerprint=fingerprint,
            )
        except IdempotencyConflict:
            return _error_response(
                frame.request_id,
                "idempotency_conflict",
                "idempotency key is bound to a different request",
            )
        except StaleWriterGeneration:
            return _error_response(
                frame.request_id,
                "gateway_generation_stale",
                "Gateway writer generation is no longer current",
            )
        except Exception:  # noqa: BLE001 - state exception details are never a wire response.
            return _error_response(
                frame.request_id,
                "state_unavailable",
                "Gateway durable state is unavailable",
            )

        if reservation.state == "completed":
            try:
                return _response_from_state(
                    frame.request_id,
                    reservation.response,
                    max_bytes=self.config.max_frame_bytes,
                )
            except GatewayStateError:
                return _error_response(
                    frame.request_id,
                    "state_unavailable",
                    "Gateway durable state is unavailable",
                )
        if reservation.state == "pending":
            return await self._reconcile_or_retry_mutation(
                frame,
                client,
                fingerprint=fingerprint,
                recovery_required=False,
            )
        if reservation.state == "recovery_required":
            return await self._reconcile_or_retry_mutation(
                frame,
                client,
                fingerprint=fingerprint,
                recovery_required=True,
            )

        response = await self._dispatch(frame, client)
        return self._complete_mutation(frame, client, fingerprint, response)

    async def _reconcile_or_retry_mutation(
        self,
        frame: RequestFrame,
        client: GatewayClientContext,
        *,
        fingerprint: str,
        recovery_required: bool,
    ) -> ResponseFrame:
        if frame.method != "chat.send":
            code = "idempotency_recovery_required" if recovery_required else "request_in_progress"
            message = (
                "interrupted mutation requires explicit recovery"
                if recovery_required
                else "an identical mutation is already in progress"
            )
            return _error_response(frame.request_id, code, message)

        reconciler = getattr(self.dispatcher, "reconcile_mutation", None)
        if not callable(reconciler):
            code = "idempotency_recovery_required" if recovery_required else "request_in_progress"
            message = (
                "interrupted mutation requires explicit recovery"
                if recovery_required
                else "an identical mutation is already in progress"
            )
            return _error_response(
                frame.request_id,
                code,
                message,
            )
        try:
            reconciliation = await asyncio.wait_for(
                reconciler(frame, client=client),
                timeout=self.config.request_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reconciliation failures fail closed.
            return _error_response(
                frame.request_id,
                "mutation_reconciliation_failed",
                "mutation state could not be reconciled",
            )
        if not isinstance(reconciliation, GatewayMutationReconciliation):
            return _error_response(
                frame.request_id,
                "mutation_reconciliation_failed",
                "mutation state could not be reconciled",
            )

        if recovery_required:
            try:
                self.state_store.resolve_idempotency_recovery(
                    generation=self.server_generation,
                    client_id=client.client_id,
                    method=frame.method,
                    key=frame.idempotency_key or "",
                    request_fingerprint=fingerprint,
                    resolution="retry",
                )
            except Exception:  # noqa: BLE001 - state details never cross the wire.
                return _error_response(
                    frame.request_id,
                    "state_unavailable",
                    "Gateway durable state is unavailable",
                )

        if reconciliation.state == "domain_not_started":
            response = await self._dispatch(frame, client)
        else:
            response = ResponseFrame(
                request_id=frame.request_id,
                ok=True,
                result=dict(reconciliation.result or {}),
            )
            try:
                encode_frame(response, max_bytes=self.config.max_frame_bytes)
            except GatewayProtocolError:
                return _error_response(
                    frame.request_id,
                    "mutation_reconciliation_failed",
                    "mutation state could not be reconciled",
                )
        return self._complete_mutation(frame, client, fingerprint, response)

    def _complete_mutation(
        self,
        frame: RequestFrame,
        client: GatewayClientContext,
        fingerprint: str,
        response: ResponseFrame,
    ) -> ResponseFrame:
        key = frame.idempotency_key
        if key is None:
            raise AssertionError("validated mutation is missing its idempotency key")
        if (
            not response.ok
            and response.error is not None
            and response.error.code in {"request_timeout", "mutation_outcome_unknown"}
        ):
            return _error_response(
                frame.request_id,
                "mutation_outcome_unknown",
                "mutation may have completed but its durable outcome is unknown",
            )
        try:
            self.state_store.complete_idempotency(
                generation=self.server_generation,
                client_id=client.client_id,
                method=frame.method,
                key=key,
                request_fingerprint=fingerprint,
                response=_response_to_state(response),
            )
        except Exception:  # noqa: BLE001 - mutation outcome is unknown across any state failure.
            return _error_response(
                frame.request_id,
                "mutation_outcome_unknown",
                "mutation may have completed but its durable response was not recorded",
            )
        return response

    async def _dispatch(
        self,
        frame: RequestFrame,
        client: GatewayClientContext,
    ) -> ResponseFrame:
        try:
            result = await asyncio.wait_for(
                self.dispatcher.dispatch(frame, client=client),
                timeout=self.config.request_timeout_seconds,
            )
            if not isinstance(result, Mapping):
                raise TypeError("Gateway dispatcher result must be a mapping")
            response = ResponseFrame(
                request_id=frame.request_id,
                ok=True,
                result=dict(result),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            response = _error_response(
                frame.request_id,
                "request_timeout",
                "Gateway request timed out; operation completion is unknown",
            )
        except GatewayDispatchError as exc:
            response = _error_response(
                frame.request_id,
                exc.code,
                exc.public_message,
                data=exc.data,
            )
        except Exception:  # noqa: BLE001 - dispatcher exception text is never a wire response.
            response = _error_response(
                frame.request_id,
                "internal_error",
                "Gateway request failed",
            )
        try:
            encode_frame(response, max_bytes=self.config.max_frame_bytes)
        except GatewayProtocolError:
            return _error_response(
                frame.request_id,
                "internal_error",
                "Gateway request failed",
            )
        return response

    async def _send_events(self, context: _ConnectionState) -> None:
        try:
            while True:
                frame = await context.event_queue.get()
                await self._send_frame(context, frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - close with a stable reason only.
            await self._safe_close(context, 1011, "event_delivery_error")

    async def _send_frame(
        self,
        context: _ConnectionState,
        frame: EventFrame | ResponseFrame,
    ) -> None:
        encoded = encode_frame(frame, max_bytes=self.config.max_frame_bytes).decode("utf-8")
        async with context.send_lock:
            await context.websocket.send(encoded)

    async def _safe_close(self, context: _ConnectionState, code: int, reason: str) -> None:
        context.closing = True
        await _close_websocket(context.websocket, code, reason)

    async def _shutdown_connection(self, context: _ConnectionState) -> None:
        context.closing = True
        event_task = context.event_task
        if event_task is not None and event_task is not asyncio.current_task():
            event_task.cancel()
        requests = tuple(context.inflight_tasks)
        if self._stopping:
            for task in requests:
                task.cancel()
        waiting: list[asyncio.Task[None]] = list(requests)
        if event_task is not None and event_task is not asyncio.current_task():
            waiting.append(event_task)
        if waiting:
            await asyncio.gather(*waiting, return_exceptions=True)

    def _schedule_maintenance(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._maintenance_tasks.add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            self._maintenance_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    def _is_current_generation(self) -> bool:
        try:
            return self.state_store.current_writer_generation() == self.server_generation
        except Exception:  # noqa: BLE001 - callers receive only the stable stale-state outcome.
            return False

    def _require_current_generation(self) -> None:
        if not self._is_current_generation():
            raise GatewayServerLifecycleError("Gateway writer generation is not current")

    def _require_owner_loop(self) -> None:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise GatewayServerLifecycleError(
                "Gateway operation requires its owning event loop"
            ) from exc
        if self._owner_loop is not current:
            raise GatewayServerLifecycleError("Gateway operation used a different event loop")

    def _current_event_cursor(self) -> int:
        return self.state_store.replay_events(0, limit=1).current_cursor

    def _can_view_event(
        self,
        client: GatewayClientContext,
        frame: EventFrame,
    ) -> bool:
        if frame.event not in events_for_scopes(client.scopes):
            return False
        try:
            return self.event_visibility_policy.can_view(client=client, event=frame) is True
        except Exception:  # noqa: BLE001 - visibility failures deny without a wire detail.
            return False


async def _close_websocket(websocket: Any, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - closing is best effort and has no response surface.
        pass


def _protocol_error_response(request_id: str, code: str) -> ResponseFrame:
    return _error_response(
        request_id,
        code if _ERROR_CODE_RE.fullmatch(code) else "invalid_request",
        _PROTOCOL_ERROR_MESSAGES.get(code, "Gateway request was rejected"),
    )


def _error_response(
    request_id: str,
    code: str,
    message: str,
    *,
    data: Mapping[str, Any] | None = None,
) -> ResponseFrame:
    return ResponseFrame(
        request_id=request_id,
        ok=False,
        error=ResponseError(
            code=code,
            message=message,
            data=dict(data) if data is not None else None,
        ),
    )


def _response_to_state(response: ResponseFrame) -> Mapping[str, Any]:
    if response.ok:
        return {"ok": True, "result": dict(response.result or {})}
    if response.error is None:
        raise GatewayStateError("failed response has no durable error")
    error: dict[str, Any] = {
        "code": response.error.code,
        "message": response.error.message,
    }
    if response.error.data is not None:
        error["data"] = dict(response.error.data)
    return {"ok": False, "error": error}


def _response_from_state(
    request_id: str,
    stored: Mapping[str, Any] | None,
    *,
    max_bytes: int,
) -> ResponseFrame:
    try:
        if stored is None or type(stored.get("ok")) is not bool:
            raise ValueError
        if stored["ok"] is True:
            if set(stored) != {"ok", "result"} or not isinstance(stored["result"], Mapping):
                raise ValueError
            response = ResponseFrame(
                request_id=request_id,
                ok=True,
                result=dict(stored["result"]),
            )
        else:
            if set(stored) != {"ok", "error"} or not isinstance(stored["error"], Mapping):
                raise ValueError
            raw_error = stored["error"]
            if set(raw_error).difference({"code", "message", "data"}):
                raise ValueError
            code = raw_error.get("code")
            message = raw_error.get("message")
            data = raw_error.get("data")
            if not isinstance(code, str) or not isinstance(message, str):
                raise ValueError
            if data is not None and not isinstance(data, Mapping):
                raise ValueError
            response = _error_response(
                request_id,
                code,
                message,
                data=data,
            )
        encode_frame(response, max_bytes=max_bytes)
        return response
    except (GatewayProtocolError, KeyError, TypeError, ValueError) as exc:
        raise GatewayStateError("stored idempotency response is invalid") from exc


def _positive_finite(value: float, label: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be positive and finite")


def _bounded_int(value: int, label: str, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")


__all__ = [
    "GatewayClientContext",
    "GatewayDispatchError",
    "GatewayEventVisibilityPolicy",
    "GatewayMutationReconciliation",
    "GatewayPublishResult",
    "GatewayReplayBatch",
    "GatewayRequestDispatcher",
    "GatewayServerConfig",
    "GatewayServerHealth",
    "GatewayServerLifecycleError",
    "GatewayWebSocketServer",
]
