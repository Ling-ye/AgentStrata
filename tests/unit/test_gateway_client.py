from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("websockets")

from chatcopilot.contracts.gateway_protocol import EventFrame, RequestFrame
from chatcopilot.contracts.gateway_rpc import (
    ChatFinalEvent,
    ChatSendParams,
    ChatUpdateEvent,
    EventsReplayParams,
    EventsReplayResult,
    GatewayReplayItem,
    HealthParams,
    HealthResult,
    StatusParams,
    StatusResult,
    TextRpcSegment,
)
from chatcopilot.gateway.protocol import (
    GatewayCredentialBinding,
    GatewayProtocolError,
    StaticGatewayCredentialAuthority,
)
from chatcopilot.gateway.rpc_validation import (
    parse_event_payload,
    parse_method_result,
    parse_request_params,
    serialize_event_payload,
    serialize_method_result,
)
from chatcopilot.gateway.server import (
    GatewayClientContext,
    GatewayRequestDispatcher,
    GatewayServerConfig,
    GatewayWebSocketServer,
)
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.protocols.gateway_client import (
    GatewayClientConfig,
    GatewayClientError,
    GatewayConnectionClosed,
    GatewayMutationOutcomeUnknown,
    GatewayRecoveryRequired,
    GatewayRemoteError,
    GatewayWebSocketClient,
)


TOKEN = "c" * 32
SCOPES = ("gateway.read", "chat.write", "chat.abort")


class AllowEvents:
    def can_view(self, *, client: GatewayClientContext, event: EventFrame) -> bool:
        del client, event
        return True


class TypedDispatcher:
    def __init__(self) -> None:
        self.server: GatewayWebSocketServer | None = None
        self.calls: list[str] = []

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        self.calls.append(request.method)
        params = parse_request_params(request.method, request.params)
        if request.method == "health":
            assert isinstance(params, HealthParams)
            return serialize_method_result(
                request.method,
                HealthResult(ready=True, server_generation=1, event_cursor=0),
            )
        if request.method == "status":
            assert isinstance(params, StatusParams)
            await asyncio.sleep(0)
            return serialize_method_result(
                request.method,
                StatusResult(
                    ready=True,
                    server_generation=1,
                    event_cursor=0,
                    active_runs=0,
                    session_count=0,
                ),
            )
        if request.method == "events.replay":
            assert isinstance(params, EventsReplayParams)
            assert self.server is not None
            replay = self.server.replay_events(
                after_seq=params.after_seq,
                client=client,
                limit=params.limit,
            )
            result = EventsReplayResult(
                events=tuple(
                    GatewayReplayItem(
                        event=event.event,
                        seq=event.seq,
                        payload=parse_event_payload(event.event, event.payload),
                    )
                    for event in replay.events
                ),
                next_cursor=(
                    replay.current_cursor if replay.resync_required else replay.next_cursor
                ),
                current_cursor=replay.current_cursor,
                resync_required=replay.resync_required,
            )
            return serialize_method_result(request.method, result)
        raise AssertionError(f"unexpected method: {request.method}")


class BlockingMutationDispatcher(TypedDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        if request.method != "chat.send":
            return await super().dispatch(request, client=client)
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


@asynccontextmanager
async def _running_server(
    tmp_path: Path,
    dispatcher: GatewayRequestDispatcher,
) -> AsyncIterator[tuple[GatewayWebSocketServer, GatewayStateStore]]:
    store = GatewayStateStore(tmp_path / "gateway-state")
    generation = store.acquire_writer_generation()
    authority = StaticGatewayCredentialAuthority(
        (
            GatewayCredentialBinding(
                token=TOKEN,
                client_id="acp-edge",
                client_mode="acp",
                scopes=SCOPES,
            ),
        )
    )
    server = GatewayWebSocketServer(
        config=GatewayServerConfig(replay_limit=100),
        dispatcher=dispatcher,
        credential_authority=authority,
        event_visibility_policy=AllowEvents(),
        state_store=store,
        server_generation=generation,
    )
    if isinstance(dispatcher, TypedDispatcher):
        dispatcher.server = server
    await server.start()
    try:
        yield server, store
    finally:
        await server.stop()


def _client(server: GatewayWebSocketServer, **overrides: Any) -> GatewayWebSocketClient:
    return GatewayWebSocketClient(
        GatewayClientConfig(
            url=server.url,
            token=TOKEN,
            **overrides,
        )
    )


def test_config_is_loopback_only_scope_exact_and_redacts_token() -> None:
    config = GatewayClientConfig(url="ws://127.0.0.1:8765", token=TOKEN)
    assert TOKEN not in repr(config)
    with pytest.raises(ValueError, match="loopback"):
        GatewayClientConfig(url="ws://example.invalid:8765", token=TOKEN)
    with pytest.raises(ValueError, match="loopback"):
        GatewayClientConfig(url="ws://127.0.0.1:0", token=TOKEN)
    with pytest.raises(ValueError, match="32-128"):
        GatewayClientConfig(url="ws://127.0.0.1:8765", token="weak")


def test_client_rejects_cross_event_loop_reuse_with_stable_error() -> None:
    client = GatewayWebSocketClient(
        GatewayClientConfig(url="ws://127.0.0.1:8765", token=TOKEN)
    )

    async def bind_client() -> None:
        client._owner_loop = asyncio.get_running_loop()

    asyncio.run(bind_client())

    async def reject_reuse() -> None:
        with pytest.raises(GatewayClientError) as raised:
            await client.connect()
        assert raised.value.code == "event_loop_mismatch"

    asyncio.run(reject_reuse())


def test_event_replay_rpc_is_closed_typed_and_rejects_false_resync() -> None:
    payload = ChatUpdateEvent("session-a", "run-a", "hello")
    result = EventsReplayResult(
        events=(GatewayReplayItem("chat.update", 3, payload),),
        next_cursor=3,
        current_cursor=4,
        resync_required=False,
    )
    encoded = serialize_method_result("events.replay", result)
    assert parse_method_result("events.replay", encoded) == result

    with pytest.raises(GatewayProtocolError, match="resync"):
        parse_method_result(
            "events.replay",
            {
                **encoded,
                "resyncRequired": True,
            },
        )
    with pytest.raises(GatewayProtocolError, match="fields"):
        parse_method_result("events.replay", {**encoded, "private": "value"})


def test_real_loopback_handshake_correlates_typed_concurrent_requests(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = TypedDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            client = _client(server)
            hello = await client.connect()
            assert hello.client_id == "acp-edge"
            assert hello.scopes == SCOPES
            assert "events.replay" in hello.methods
            health, status = await asyncio.gather(
                client.request("health", HealthParams()),
                client.request("status", StatusParams()),
            )
            assert isinstance(health, HealthResult) and health.ready
            assert isinstance(status, StatusResult) and status.ready
            with pytest.raises(GatewayClientError) as invalid:
                await client.request(
                    "chat.send",
                    ChatSendParams(
                        session_id="session-a",
                        segments=(TextRpcSegment("😀" * 65_536),),
                    ),
                    idempotency_key="oversized-chat",
                )
            assert invalid.value.code == "rpc_too_large"
            assert "chat.send" not in dispatcher.calls
            await client.close()

    asyncio.run(scenario())


def test_authentication_error_and_repr_do_not_expose_the_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = TypedDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            client = GatewayWebSocketClient(GatewayClientConfig(url=server.url, token="x" * 32))
            with pytest.raises(GatewayRemoteError) as raised:
                await client.connect()
            assert raised.value.code == "authentication_failed"
            assert TOKEN not in repr(raised.value)
            assert "x" * 32 not in repr(raised.value)

    asyncio.run(scenario())


def test_live_events_are_strictly_parsed_and_session_filtered(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = TypedDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, store):
            client = _client(server)
            await client.connect()
            subscription = client.subscribe(
                events={"chat.update"},
                session_id="session-a",
            )
            generation = store.current_writer_generation()
            other = ChatUpdateEvent(session_id="session-b", run_id="run-b", text="other")
            selected = ChatUpdateEvent(session_id="session-a", run_id="run-a", text="hello")
            for payload in (other, selected):
                record = store.append_event(
                    generation=generation,
                    event="chat.update",
                    payload=serialize_event_payload("chat.update", payload),
                )
                server.publish(EventFrame(record.event, record.seq, record.payload))
            delivered = await asyncio.wait_for(subscription.get(), timeout=1.0)
            assert delivered.payload == selected
            subscription.close()
            waiting = client.subscribe(events={"chat.final"}, session_id="session-a")
            pending_get = asyncio.create_task(waiting.get())
            await asyncio.sleep(0)
            waiting.close()
            with pytest.raises(GatewayConnectionClosed) as closed:
                await asyncio.wait_for(pending_get, timeout=1.0)
            assert closed.value.code == "subscription_closed"
            await client.close()

    asyncio.run(scenario())


def test_reconnect_replays_cursor_without_reissuing_mutations(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = TypedDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, store):
            client = _client(server)
            await client.connect()
            generation = store.current_writer_generation()
            first_payload = ChatFinalEvent(
                session_id="session-a",
                run_id="run-a",
                stop_reason="completed",
                segments=(TextRpcSegment("first"),),
            )
            first = store.append_event(
                generation=generation,
                event="chat.final",
                payload=serialize_event_payload("chat.final", first_payload),
            )
            server.publish(EventFrame(first.event, first.seq, first.payload))
            await asyncio.sleep(0.05)
            assert client.event_cursor == first.seq
            await client.close()

            second_payload = ChatFinalEvent(
                session_id="session-b",
                run_id="run-b",
                stop_reason="completed",
                segments=(TextRpcSegment("second"),),
            )
            second = store.append_event(
                generation=generation,
                event="chat.final",
                payload=serialize_event_payload("chat.final", second_payload),
            )
            await client.connect()
            assert client.event_cursor == second.seq
            assert dispatcher.calls.count("events.replay") == 1
            assert "chat.send" not in dispatcher.calls
            await client.close()

    asyncio.run(scenario())


def test_pruned_event_history_requires_resync_and_never_claims_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = TypedDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, store):
            generation = store.current_writer_generation()
            for index in range(2):
                payload = ChatUpdateEvent(
                    session_id="session-a",
                    run_id="run-a",
                    text=f"update-{index}",
                )
                store.append_event(
                    generation=generation,
                    event="chat.update",
                    payload=serialize_event_payload("chat.update", payload),
                )
            store.prune_events(generation=generation, retain_last=1)
            client = _client(server, resume_event_cursor=0)
            with pytest.raises(GatewayRecoveryRequired) as raised:
                await client.connect()
            assert raised.value.code == "event_resync_required"
            assert not client.connected

    asyncio.run(scenario())


def test_disconnect_fails_pending_mutation_as_outcome_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        dispatcher = BlockingMutationDispatcher()
        async with _running_server(tmp_path, dispatcher) as (server, _store):
            client = _client(server)
            await client.connect()
            request = asyncio.create_task(
                client.request(
                    "chat.send",
                    ChatSendParams(
                        session_id="session-a",
                        segments=(TextRpcSegment("hello"),),
                    ),
                    idempotency_key="chat-send-once",
                )
            )
            await asyncio.wait_for(dispatcher.started.wait(), timeout=1.0)
            await server.stop()
            with pytest.raises(GatewayMutationOutcomeUnknown):
                await asyncio.wait_for(request, timeout=1.0)
            await client.close()

    asyncio.run(scenario())
