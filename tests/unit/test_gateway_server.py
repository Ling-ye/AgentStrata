from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")
from websockets.exceptions import ConnectionClosed, WebSocketException  # noqa: E402

from chatcopilot.contracts.gateway_protocol import (  # noqa: E402
    EventFrame,
    GatewayScope,
    RequestFrame,
    ResponseFrame,
)
from chatcopilot.gateway.protocol import (  # noqa: E402
    GatewayCredentialBinding,
    StaticGatewayCredentialAuthority,
    decode_frame,
    encode_frame,
    request_fingerprint,
)
from chatcopilot.gateway.server import (  # noqa: E402
    GatewayClientContext,
    GatewayEventVisibilityPolicy,
    GatewayRequestDispatcher,
    GatewayServerConfig,
    GatewayServerLifecycleError,
    GatewayWebSocketServer,
)
from chatcopilot.gateway.state_store import GatewayStateStore  # noqa: E402


TOKEN = "s" * 32
_CREDENTIAL_TOKENS: Mapping[tuple[str, str, frozenset[str]], str] = {
    ("acp-edge", "acp", frozenset({"gateway.read"})): TOKEN,
    ("acp-edge", "acp", frozenset({"gateway.read", "chat.write"})): "d" * 32,
    ("acp-edge", "acp", frozenset({"chat.write"})): "w" * 32,
    ("reader", "acp", frozenset({"gateway.read"})): "r" * 32,
    ("approver", "acp", frozenset({"approvals.respond"})): "a" * 32,
    ("acp-a", "acp", frozenset({"gateway.read"})): "u" * 32,
    ("acp-b", "acp", frozenset({"gateway.read"})): "v" * 32,
    ("approval-viewer", "acp", frozenset({"approvals.respond"})): "p" * 32,
    ("approval-denied", "acp", frozenset({"approvals.respond"})): "q" * 32,
}
_SCOPE_ORDER: tuple[GatewayScope, ...] = (
    "gateway.read",
    "chat.write",
    "chat.abort",
    "approvals.respond",
    "gateway.admin",
)


class ChannelStatusVisibilityPolicy:
    def can_view(
        self,
        *,
        client: GatewayClientContext,
        event: EventFrame,
    ) -> bool:
        del client
        return event.event == "channel.status"


class ScopedEventVisibilityPolicy:
    def __init__(
        self,
        *,
        sessions: Mapping[str, frozenset[str]] | None = None,
        approvals: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self.sessions = sessions or {}
        self.approvals = approvals or {}

    def can_view(
        self,
        *,
        client: GatewayClientContext,
        event: EventFrame,
    ) -> bool:
        if event.event == "channel.status":
            return True
        if event.event == "approval.requested":
            approval_id = event.payload.get("approvalId")
            session_id = event.payload.get("sessionId")
            return (
                isinstance(approval_id, str)
                and isinstance(session_id, str)
                and approval_id in self.approvals.get(client.client_id, frozenset())
                and session_id in self.sessions.get(client.client_id, frozenset())
            )
        session_id = event.payload.get("sessionId")
        return isinstance(session_id, str) and session_id in self.sessions.get(
            client.client_id,
            frozenset(),
        )


class RaisingVisibilityPolicy:
    def can_view(
        self,
        *,
        client: GatewayClientContext,
        event: EventFrame,
    ) -> bool:
        del client, event
        raise RuntimeError("private-policy-secret")


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[RequestFrame, GatewayClientContext]] = []

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        self.calls.append((request, client))
        return {"accepted": True, "method": request.method}


class FailingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        del request, client
        self.calls += 1
        raise RuntimeError("private-dispatcher-secret")


class BlockingDispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        del client
        self.started.set()
        await self.release.wait()
        return {"requestId": request.request_id}


class TimeoutDispatcher:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        del request, client
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class DisconnectDispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        del request, client
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
            self.completed.set()
            return {"completed": True}
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _credential_authority() -> StaticGatewayCredentialAuthority:
    credentials = tuple(
        GatewayCredentialBinding(
            token=token,
            client_id=client_id,
            client_mode=client_mode,
            scopes=tuple(scope for scope in _SCOPE_ORDER if scope in scopes),
        )
        for (client_id, client_mode, scopes), token in _CREDENTIAL_TOKENS.items()
    )
    return StaticGatewayCredentialAuthority(credentials)


def _token_for(
    client_id: str,
    client_mode: str,
    scopes: tuple[str, ...],
) -> str:
    return _CREDENTIAL_TOKENS[(client_id, client_mode, frozenset(scopes))]


@asynccontextmanager
async def _running_server(
    tmp_path: Path,
    dispatcher: GatewayRequestDispatcher,
    *,
    event_visibility_policy: GatewayEventVisibilityPolicy | None = None,
    **config_overrides: Any,
) -> AsyncIterator[tuple[GatewayWebSocketServer, GatewayStateStore]]:
    store = GatewayStateStore(tmp_path / "gateway-state")
    generation = store.acquire_writer_generation()
    server = GatewayWebSocketServer(
        config=GatewayServerConfig(**config_overrides),
        dispatcher=dispatcher,
        credential_authority=_credential_authority(),
        event_visibility_policy=(event_visibility_policy or ChannelStatusVisibilityPolicy()),
        state_store=store,
        server_generation=generation,
    )
    await server.start()
    try:
        yield server, store
    finally:
        await server.stop()


async def _open_websocket(url: str) -> Any:
    options = {"compression": None, "open_timeout": 1.0}
    try:
        return await websockets.connect(url, proxy=None, **options)
    except TypeError:
        return await websockets.connect(url, **options)


async def _send(websocket: Any, frame: RequestFrame) -> None:
    await websocket.send(encode_frame(frame).decode("utf-8"))


async def _receive(websocket: Any) -> EventFrame | ResponseFrame:
    frame = decode_frame(await websocket.recv())
    assert isinstance(frame, (EventFrame, ResponseFrame))
    return frame


async def _connect(
    server: GatewayWebSocketServer,
    *,
    client_id: str = "acp-edge",
    client_mode: str = "acp",
    token: str | None = None,
    scopes: tuple[str, ...] = ("gateway.read",),
) -> tuple[Any, ResponseFrame]:
    supplied_token = token or _token_for(client_id, client_mode, scopes)
    websocket = await _open_websocket(server.url)
    challenge = await _receive(websocket)
    assert isinstance(challenge, EventFrame)
    assert challenge.event == "connect.challenge"
    nonce = challenge.payload["nonce"]
    assert isinstance(nonce, str)
    await _send(
        websocket,
        RequestFrame(
            request_id="connect-1",
            method="connect",
            params={
                "nonce": nonce,
                "minProtocol": 1,
                "maxProtocol": 1,
                "client": {"id": client_id, "version": "0.1", "mode": client_mode},
                "scopes": list(scopes),
                "capabilities": ["event-replay"],
                "auth": {"token": supplied_token},
            },
        ),
    )
    response = await _receive(websocket)
    assert isinstance(response, ResponseFrame)
    return websocket, response


def test_server_config_is_loopback_only_and_bounded() -> None:
    GatewayServerConfig()
    wildcard_host = ".".join(("0", "0", "0", "0"))
    with pytest.raises(ValueError, match="exactly"):
        GatewayServerConfig(host=wildcard_host)
    with pytest.raises(ValueError, match="port"):
        GatewayServerConfig(port=65536)
    with pytest.raises(ValueError, match="per-connection"):
        GatewayServerConfig(
            max_inflight_requests_per_connection=2,
            max_total_inflight_requests=1,
        )


def test_server_refuses_to_start_with_a_stale_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = GatewayStateStore(tmp_path / "state")
        stale = store.acquire_writer_generation()
        store.acquire_writer_generation()
        server = GatewayWebSocketServer(
            config=GatewayServerConfig(),
            dispatcher=RecordingDispatcher(),
            credential_authority=_credential_authority(),
            event_visibility_policy=ChannelStatusVisibilityPolicy(),
            state_store=store,
            server_generation=stale,
        )
        with pytest.raises(GatewayServerLifecycleError, match="not current"):
            await server.start()

    _run(scenario())


def test_authentication_failure_and_wrong_first_frame_close_the_connection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with _running_server(tmp_path, RecordingDispatcher()) as (server, _store):
            failed, response = await _connect(server, token="x" * 32)
            assert not response.ok
            assert response.error is not None
            assert response.error.code == "authentication_failed"
            with pytest.raises(ConnectionClosed):
                await failed.recv()
            assert failed.close_code == 1008

            wrong_first = await _open_websocket(server.url)
            challenge = await _receive(wrong_first)
            assert isinstance(challenge, EventFrame)
            await _send(wrong_first, RequestFrame("wrong-1", "health", {}))
            rejected = await _receive(wrong_first)
            assert isinstance(rejected, ResponseFrame)
            assert rejected.error is not None
            assert rejected.error.code == "connect_required"
            with pytest.raises(ConnectionClosed):
                await wrong_first.recv()
            assert wrong_first.close_code == 1008

    _run(scenario())


def test_credentials_reject_cross_client_mode_scope_upgrade_and_unknown_token(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with _running_server(tmp_path, RecordingDispatcher()) as (server, _store):
            attempts = (
                ("reader", "acp", ("gateway.read",), TOKEN),
                ("acp-edge", "console", ("gateway.read",), TOKEN),
                (
                    "acp-edge",
                    "acp",
                    ("gateway.read", "chat.write"),
                    TOKEN,
                ),
                ("acp-edge", "acp", ("gateway.read",), "z" * 32),
            )
            for client_id, client_mode, scopes, token in attempts:
                websocket, response = await _connect(
                    server,
                    client_id=client_id,
                    client_mode=client_mode,
                    scopes=scopes,
                    token=token,
                )
                assert not response.ok
                assert response.error is not None
                assert response.error.code == "authentication_failed"
                assert response.error.message == "Gateway authentication failed"
                serialized = repr(response)
                assert TOKEN not in serialized
                assert "z" * 32 not in serialized
                with pytest.raises(ConnectionClosed):
                    await websocket.recv()

            valid, hello = await _connect(server)
            assert hello.ok
            await valid.close()

    _run(scenario())


def test_first_frame_has_a_hard_handshake_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _running_server(
            tmp_path,
            RecordingDispatcher(),
            handshake_timeout_seconds=0.05,
        ) as (server, _store):
            websocket = await _open_websocket(server.url)
            challenge = await _receive(websocket)
            assert isinstance(challenge, EventFrame)
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(websocket.recv(), timeout=1.0)
            assert websocket.close_code == 1008
            assert websocket.close_reason == "handshake_timeout"

    _run(scenario())


def test_scope_gate_and_mutation_idempotency_replay_and_drift(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = RecordingDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            websocket, hello = await _connect(
                server,
                scopes=("gateway.read", "chat.write"),
            )
            assert hello.ok

            await _send(websocket, RequestFrame("scope-1", "approvals.list", {}))
            denied = await _receive(websocket)
            assert isinstance(denied, ResponseFrame)
            assert denied.error is not None
            assert denied.error.code == "scope_denied"

            await _send(websocket, RequestFrame("missing-1", "chat.send", {"text": "hello"}))
            missing = await _receive(websocket)
            assert isinstance(missing, ResponseFrame)
            assert missing.error is not None
            assert missing.error.code == "idempotency_key_required"

            original = RequestFrame(
                "mutation-1",
                "chat.send",
                {"sessionId": "session-1", "text": "hello"},
                "stable-key",
            )
            await _send(websocket, original)
            accepted = await _receive(websocket)
            assert isinstance(accepted, ResponseFrame)
            assert accepted.ok

            await _send(
                websocket,
                RequestFrame(
                    "mutation-2",
                    original.method,
                    original.params,
                    original.idempotency_key,
                ),
            )
            replayed = await _receive(websocket)
            assert isinstance(replayed, ResponseFrame)
            assert replayed.ok
            assert replayed.request_id == "mutation-2"
            assert replayed.result == accepted.result

            await _send(
                websocket,
                RequestFrame(
                    "mutation-3",
                    "chat.send",
                    {"sessionId": "session-1", "text": "changed"},
                    "stable-key",
                ),
            )
            conflict = await _receive(websocket)
            assert isinstance(conflict, ResponseFrame)
            assert conflict.error is not None
            assert conflict.error.code == "idempotency_conflict"
            assert [request.method for request, _client in dispatcher.calls] == ["chat.send"]
            await websocket.close()

    _run(scenario())


def test_recovery_required_is_explicit_and_never_auto_replayed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = GatewayStateStore(tmp_path / "state")
        first = store.acquire_writer_generation()
        request = RequestFrame(
            "old-request",
            "chat.send",
            {"sessionId": "session-1", "text": "hello"},
            "recovery-key",
        )
        store.reserve_idempotency(
            generation=first,
            client_id="acp-edge",
            method=request.method,
            key=request.idempotency_key or "",
            request_fingerprint=request_fingerprint(request),
        )
        current = store.acquire_writer_generation()
        dispatcher = RecordingDispatcher()
        server = GatewayWebSocketServer(
            config=GatewayServerConfig(),
            dispatcher=dispatcher,
            credential_authority=_credential_authority(),
            event_visibility_policy=ChannelStatusVisibilityPolicy(),
            state_store=store,
            server_generation=current,
        )
        await server.start()
        try:
            websocket, hello = await _connect(server, scopes=("chat.write",))
            assert hello.ok
            await _send(
                websocket,
                RequestFrame(
                    "new-request",
                    request.method,
                    request.params,
                    request.idempotency_key,
                ),
            )
            response = await _receive(websocket)
            assert isinstance(response, ResponseFrame)
            assert response.error is not None
            assert response.error.code == "idempotency_recovery_required"
            assert dispatcher.calls == []
            await websocket.close()
        finally:
            await server.stop()

    _run(scenario())


def test_request_tasks_are_bounded_per_connection(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = BlockingDispatcher()
        async with _running_server(
            tmp_path,
            dispatcher,
            max_inflight_requests_per_connection=1,
            max_total_inflight_requests=1,
        ) as (server, _store):
            websocket, hello = await _connect(server)
            assert hello.ok
            await _send(websocket, RequestFrame("request-1", "health", {}))
            await asyncio.wait_for(dispatcher.started.wait(), timeout=1.0)
            await _send(websocket, RequestFrame("request-2", "health", {}))
            rejected = await _receive(websocket)
            assert isinstance(rejected, ResponseFrame)
            assert rejected.request_id == "request-2"
            assert rejected.error is not None
            assert rejected.error.code == "too_many_requests"
            dispatcher.release.set()
            completed = await _receive(websocket)
            assert isinstance(completed, ResponseFrame)
            assert completed.request_id == "request-1"
            assert completed.ok
            await websocket.close()

    _run(scenario())


def test_request_timeout_cancels_only_the_transport_dispatch_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = TimeoutDispatcher()
        async with _running_server(
            tmp_path,
            dispatcher,
            request_timeout_seconds=0.05,
        ) as (server, _store):
            websocket, hello = await _connect(server)
            assert hello.ok
            await _send(websocket, RequestFrame("timeout-1", "health", {}))
            response = await _receive(websocket)
            assert isinstance(response, ResponseFrame)
            assert response.error is not None
            assert response.error.code == "request_timeout"
            assert "unknown" in response.error.message
            await asyncio.wait_for(dispatcher.cancelled.wait(), timeout=1.0)
            await websocket.close()

    _run(scenario())


def test_disconnect_preserves_bounded_mutation_for_idempotent_recovery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        dispatcher = DisconnectDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            websocket, hello = await _connect(server, scopes=("chat.write",))
            assert hello.ok
            request = RequestFrame(
                "disconnect-1",
                "chat.send",
                {"sessionId": "session-1", "text": "hello"},
                "disconnect-key",
            )
            await _send(websocket, request)
            await asyncio.wait_for(dispatcher.started.wait(), timeout=1.0)
            await websocket.close()
            dispatcher.release.set()
            await asyncio.wait_for(dispatcher.completed.wait(), timeout=1.0)
            assert not dispatcher.cancelled.is_set()

            reconnected, reconnected_hello = await _connect(
                server,
                scopes=("chat.write",),
            )
            assert reconnected_hello.ok
            await _send(
                reconnected,
                RequestFrame(
                    "disconnect-2",
                    request.method,
                    request.params,
                    request.idempotency_key,
                ),
            )
            recovered = await _receive(reconnected)
            assert isinstance(recovered, ResponseFrame)
            assert recovered.ok
            assert recovered.result == {"completed": True}
            assert dispatcher.calls == 1
            await reconnected.close()

    _run(scenario())


def test_dispatcher_exception_is_redacted_and_its_terminal_response_replays(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        dispatcher = FailingDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            websocket, hello = await _connect(server, scopes=("chat.write",))
            assert hello.ok
            request = RequestFrame(
                "failure-1",
                "chat.send",
                {"sessionId": "session-1", "text": "hello"},
                "failure-key",
            )
            await _send(websocket, request)
            raw = await websocket.recv()
            assert "private-dispatcher-secret" not in raw
            failed = decode_frame(raw)
            assert isinstance(failed, ResponseFrame)
            assert failed.error is not None
            assert failed.error.code == "internal_error"

            await _send(
                websocket,
                RequestFrame(
                    "failure-2",
                    request.method,
                    request.params,
                    request.idempotency_key,
                ),
            )
            replayed = await _receive(websocket)
            assert isinstance(replayed, ResponseFrame)
            assert replayed.error == failed.error
            assert dispatcher.calls == 1
            await websocket.close()

    _run(scenario())


def test_event_publish_filters_scopes_and_replay_advances_over_hidden_events(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        visibility = ScopedEventVisibilityPolicy(
            sessions={"approver": frozenset({"session-1"})},
            approvals={"approver": frozenset({"approval-1"})},
        )
        async with _running_server(
            tmp_path,
            RecordingDispatcher(),
            event_visibility_policy=visibility,
        ) as (server, store):
            read_socket, read_hello = await _connect(server, client_id="reader")
            approval_socket, approval_hello = await _connect(
                server,
                client_id="approver",
                scopes=("approvals.respond",),
            )
            assert read_hello.ok and approval_hello.ok
            generation = store.current_writer_generation()
            channel = store.append_event(
                generation=generation,
                event="channel.status",
                payload={"ready": True},
            )
            approval = store.append_event(
                generation=generation,
                event="approval.requested",
                payload={"approvalId": "approval-1", "sessionId": "session-1"},
            )

            approval_result = server.publish(
                EventFrame(approval.event, approval.seq, approval.payload)
            )
            assert approval_result.queued_connections == 1
            delivered = await _receive(approval_socket)
            assert delivered == EventFrame(approval.event, approval.seq, approval.payload)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read_socket.recv(), timeout=0.05)

            channel_result = server.publish(EventFrame(channel.event, channel.seq, channel.payload))
            assert channel_result.queued_connections == 1
            delivered = await _receive(read_socket)
            assert delivered == EventFrame(channel.event, channel.seq, channel.payload)

            reader = GatewayClientContext(
                client_id="reader",
                client_version="0.1",
                client_mode="acp",
                protocol=1,
                scopes=("gateway.read",),
                capabilities=("event-replay",),
            )
            read_replay = server.replay_events(after_seq=0, client=reader, limit=2)
            assert [event.seq for event in read_replay.events] == [channel.seq]
            assert read_replay.next_cursor == approval.seq
            assert read_replay.current_cursor == approval.seq

            approver = GatewayClientContext(
                client_id="approver",
                client_version="0.1",
                client_mode="acp",
                protocol=1,
                scopes=("approvals.respond",),
                capabilities=("event-replay",),
            )
            approval_replay = server.replay_events(after_seq=0, client=approver, limit=2)
            assert [event.seq for event in approval_replay.events] == [approval.seq]
            assert approval_replay.next_cursor == approval.seq

            store.prune_events(generation=generation, retain_last=1)
            pruned = server.replay_events(after_seq=0, client=reader, limit=2)
            assert pruned.resync_required
            assert pruned.events == ()
            await read_socket.close()
            await approval_socket.close()

    _run(scenario())


def test_event_visibility_is_session_bound_for_live_publish_and_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        visibility = ScopedEventVisibilityPolicy(
            sessions={
                "acp-a": frozenset({"session-a"}),
                "acp-b": frozenset({"session-b"}),
                "approval-viewer": frozenset({"session-a"}),
                "approval-denied": frozenset({"session-a"}),
            },
            approvals={"approval-viewer": frozenset({"approval-1", "approval-unbound"})},
        )
        async with _running_server(
            tmp_path,
            RecordingDispatcher(),
            event_visibility_policy=visibility,
        ) as (server, store):
            acp_a, hello_a = await _connect(server, client_id="acp-a")
            acp_b, hello_b = await _connect(server, client_id="acp-b")
            approval_viewer, approval_hello = await _connect(
                server,
                client_id="approval-viewer",
                scopes=("approvals.respond",),
            )
            approval_denied, denied_hello = await _connect(
                server,
                client_id="approval-denied",
                scopes=("approvals.respond",),
            )
            assert hello_a.ok and hello_b.ok and approval_hello.ok and denied_hello.ok
            generation = store.current_writer_generation()
            chat_a = store.append_event(
                generation=generation,
                event="chat.update",
                payload={"sessionId": "session-a", "text": "a"},
            )
            delivery_b = store.append_event(
                generation=generation,
                event="delivery.updated",
                payload={"sessionId": "session-b", "outboundId": "outbound-b"},
            )
            session_a = store.append_event(
                generation=generation,
                event="session.updated",
                payload={"sessionId": "session-a", "mode": "chat"},
            )
            approval = store.append_event(
                generation=generation,
                event="approval.requested",
                payload={"sessionId": "session-a", "approvalId": "approval-1"},
            )
            unbound_approval = store.append_event(
                generation=generation,
                event="approval.requested",
                payload={"approvalId": "approval-unbound"},
            )

            assert (
                server.publish(
                    EventFrame(chat_a.event, chat_a.seq, chat_a.payload)
                ).queued_connections
                == 1
            )
            assert await _receive(acp_a) == EventFrame(
                chat_a.event,
                chat_a.seq,
                chat_a.payload,
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(acp_b.recv(), timeout=0.05)

            assert (
                server.publish(
                    EventFrame(delivery_b.event, delivery_b.seq, delivery_b.payload)
                ).queued_connections
                == 1
            )
            assert await _receive(acp_b) == EventFrame(
                delivery_b.event,
                delivery_b.seq,
                delivery_b.payload,
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(acp_a.recv(), timeout=0.05)

            assert (
                server.publish(
                    EventFrame(approval.event, approval.seq, approval.payload)
                ).queued_connections
                == 1
            )
            assert await _receive(approval_viewer) == EventFrame(
                approval.event,
                approval.seq,
                approval.payload,
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(approval_denied.recv(), timeout=0.05)
            assert (
                server.publish(
                    EventFrame(
                        unbound_approval.event,
                        unbound_approval.seq,
                        unbound_approval.payload,
                    )
                ).queued_connections
                == 0
            )

            def client(client_id: str, scope: GatewayScope) -> GatewayClientContext:
                return GatewayClientContext(
                    client_id=client_id,
                    client_version="0.1",
                    client_mode="acp",
                    protocol=1,
                    scopes=(scope,),
                    capabilities=("event-replay",),
                )

            replay_a = server.replay_events(
                after_seq=0,
                client=client("acp-a", "gateway.read"),
                limit=5,
            )
            assert [event.seq for event in replay_a.events] == [
                chat_a.seq,
                session_a.seq,
            ]
            replay_b = server.replay_events(
                after_seq=0,
                client=client("acp-b", "gateway.read"),
                limit=5,
            )
            assert [event.seq for event in replay_b.events] == [delivery_b.seq]
            approval_replay = server.replay_events(
                after_seq=0,
                client=client("approval-viewer", "approvals.respond"),
                limit=5,
            )
            assert [event.seq for event in approval_replay.events] == [approval.seq]
            denied_replay = server.replay_events(
                after_seq=0,
                client=client("approval-denied", "approvals.respond"),
                limit=5,
            )
            assert denied_replay.events == ()
            assert denied_replay.next_cursor == unbound_approval.seq

            await acp_a.close()
            await acp_b.close()
            await approval_viewer.close()
            await approval_denied.close()

    _run(scenario())


def test_event_visibility_policy_failure_denies_publish_and_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with _running_server(
            tmp_path,
            RecordingDispatcher(),
            event_visibility_policy=RaisingVisibilityPolicy(),
        ) as (server, store):
            websocket, hello = await _connect(server, client_id="acp-a")
            assert hello.ok
            record = store.append_event(
                generation=store.current_writer_generation(),
                event="chat.update",
                payload={"sessionId": "session-a", "text": "hidden"},
            )
            result = server.publish(EventFrame(record.event, record.seq, record.payload))
            assert result.queued_connections == 0
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(websocket.recv(), timeout=0.05)
            replay = server.replay_events(
                after_seq=0,
                client=GatewayClientContext(
                    client_id="acp-a",
                    client_version="0.1",
                    client_mode="acp",
                    protocol=1,
                    scopes=("gateway.read",),
                    capabilities=("event-replay",),
                ),
            )
            assert replay.events == ()
            assert replay.next_cursor == record.seq
            await websocket.close()

    _run(scenario())


def test_slow_event_client_is_closed_when_its_queue_overflows(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _running_server(
            tmp_path,
            RecordingDispatcher(),
            max_event_queue=1,
        ) as (server, store):
            websocket, hello = await _connect(server)
            assert hello.ok
            generation = store.current_writer_generation()
            first = store.append_event(
                generation=generation,
                event="channel.status",
                payload={"sequence": 1},
            )
            second = store.append_event(
                generation=generation,
                event="channel.status",
                payload={"sequence": 2},
            )
            first_result = server.publish(EventFrame(first.event, first.seq, first.payload))
            second_result = server.publish(EventFrame(second.event, second.seq, second.payload))
            assert first_result.queued_connections == 1
            assert second_result.closed_connections == 1

            async def receive_until_closed() -> None:
                while True:
                    await websocket.recv()

            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(receive_until_closed(), timeout=1.0)
            assert websocket.close_code == 1013

    _run(scenario())


def test_stop_closes_listener_and_all_live_connections(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = RecordingDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            websocket, hello = await _connect(server)
            assert hello.ok
            assert server.health().ready
            url = server.url
            await server.stop()
            health = server.health()
            assert not health.running
            assert not health.accepting
            assert not health.ready
            assert health.active_connections == 0
            with pytest.raises(ConnectionClosed):
                await websocket.recv()
            with pytest.raises((OSError, WebSocketException)):
                await _open_websocket(url)

    _run(scenario())
