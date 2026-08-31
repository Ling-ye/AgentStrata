from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import websockets

from chatcopilot.application.sessions import SessionManager
from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
from chatcopilot.channels.qq_onebot import (
    OneBotChannelConfig,
    OneBotForwardWebSocketDriver,
)
from chatcopilot.contracts.agent import AgentResult
from chatcopilot.contracts.gateway_rpc import (
    ChatFinalEvent,
    ChatSendParams,
    SessionsCreateParams,
    SessionsCreateResult,
    TextRpcSegment,
)
from chatcopilot.contracts.identity import Identity, Role
from chatcopilot.gateway.application import GatewaySessionService
from chatcopilot.gateway.approvals import GatewayApprovalService
from chatcopilot.gateway.channels import ChannelRuntimeManager
from chatcopilot.gateway.coordinator import GatewayTurnCoordinator
from chatcopilot.gateway.dispatcher import GatewayApplicationDispatcher
from chatcopilot.gateway.events import (
    GatewayEventPublisher,
    GatewaySessionEventVisibility,
)
from chatcopilot.gateway.protocol import (
    GatewayCredentialBinding,
    StaticGatewayCredentialAuthority,
)
from chatcopilot.gateway.server import GatewayServerConfig, GatewayWebSocketServer
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.protocols.gateway_client import (
    GatewayClientConfig,
    GatewayWebSocketClient,
)


_BOT = "10001"
_ACTOR = "20002"
_GROUP = "30003"
_ONEBOT_TOKEN = "o" * 32
_GATEWAY_TOKEN = "g" * 32


class _DeterministicExecutor:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.commits: list[tuple[Any, Any]] = []

    async def execute(self, request, *, on_event, cancellation=None):
        self.requests.append(request)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        del on_event
        return SimpleNamespace(result=AgentResult("gateway-answer", "end_turn"))

    def commit_exchange(self, request, outcome, *, exchange_id=None):
        del exchange_id
        self.commits.append((request, outcome))
        return outcome

    def discard_exchange(self, request, outcome):
        del request, outcome


class _FakeOneBotProvider:
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.emit_message = asyncio.Event()
        self.outbound_received = asyncio.Event()
        self.actions: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []

    async def handler(self, websocket, *_unused: object) -> None:
        try:
            headers = getattr(getattr(websocket, "request", None), "headers", None)
            if headers is None:
                headers = getattr(websocket, "request_headers", {})
            assert headers.get("Authorization") == f"Bearer {_ONEBOT_TOKEN}"

            login = json.loads(await websocket.recv())
            assert login["action"] == "get_login_info"
            self.actions.append(login)
            await websocket.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"user_id": _BOT},
                        "echo": login["echo"],
                    }
                )
            )
            self.ready.set()
            await self.emit_message.wait()
            await websocket.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "self_id": _BOT,
                        "message_id": "message-1",
                        "group_id": _GROUP,
                        "user_id": _ACTOR,
                        "sender": {"user_id": _ACTOR, "nickname": "Actor"},
                        "message": [
                            {"type": "at", "data": {"qq": _BOT}},
                            {"type": "text", "data": {"text": "hello from qq"}},
                        ],
                    }
                )
            )

            outbound = json.loads(await websocket.recv())
            self.actions.append(outbound)
            assert outbound["action"] == "send_msg"
            await websocket.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": "provider-message-1"},
                        "echo": outbound["echo"],
                    }
                )
            )
            self.outbound_received.set()
            await websocket.wait_closed()
        except BaseException as exc:
            if not _is_normal_connection_close(exc):
                self.errors.append(exc)


def test_real_gateway_websocket_and_fake_onebot_roundtrip(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir(mode=0o700)
        workspace_root.chmod(0o700)

        provider = _FakeOneBotProvider()
        provider_server = await websockets.serve(provider.handler, "127.0.0.1", 0)
        provider_port = int(provider_server.sockets[0].getsockname()[1])

        state = GatewayStateStore(tmp_path / "gateway-state")
        generation = state.acquire_writer_generation(now=1.0)
        session_manager = SessionManager(writer_generation=generation)
        sessions = GatewaySessionService(
            state_store=state,
            session_manager=session_manager,
            generation=generation,
            client_roles={"acp-edge": Role.OWNER},
        )
        events = GatewayEventPublisher(
            state_store=state,
            sessions=sessions,
            generation=generation,
        )
        executor = _DeterministicExecutor()
        ingress_errors: list[BaseException] = []
        coordinator = GatewayTurnCoordinator(
            state_store=state,
            sessions=sessions,
            events=events,
            actor_executor=executor,
            identity_policy=IdentityPolicy.from_iterables(
                owners=(Identity(user_id=_ACTOR),)
            ),
            admission_policy=AdmissionPolicy.from_raw(
                qq_users=_ACTOR,
                qq_groups="",
                policy_version="policy-v1",
            ),
            generation=generation,
            workspace_root=workspace_root,
        )
        channels = ChannelRuntimeManager(
            state_store=state,
            application_ingress=coordinator,
            event_sink=events,
            writer_generation=generation,
        )
        async def handle_inbound(event) -> None:
            try:
                await channels.handle_inbound(event)
            except BaseException as exc:
                ingress_errors.append(exc)
                raise

        driver = OneBotForwardWebSocketDriver(
            OneBotChannelConfig(
                channel_id="qq-main",
                account_id=_BOT,
                websocket_url=f"ws://127.0.0.1:{provider_port}",
                access_token=_ONEBOT_TOKEN,
            ),
            handle_inbound,
        )
        channels.register(driver)
        coordinator.set_channel_runtime(channels)
        visibility = GatewaySessionEventVisibility(sessions)
        dispatcher = GatewayApplicationDispatcher(
            state_store=state,
            sessions=sessions,
            events=events,
            coordinator=coordinator,
            channel_runtime=channels,
            approval_service=GatewayApprovalService(state, generation=generation),
            event_visibility=visibility,
            generation=generation,
            ready=lambda: channels.health().state == "ready",
        )
        authority = StaticGatewayCredentialAuthority(
            (
                GatewayCredentialBinding(
                    token=_GATEWAY_TOKEN,
                    client_id="acp-edge",
                    client_mode="acp",
                    scopes=("gateway.read", "chat.write", "chat.abort"),
                ),
            )
        )
        gateway_server = GatewayWebSocketServer(
            config=GatewayServerConfig(
                host="127.0.0.1",
                port=0,
                policy_version="policy-v1",
            ),
            dispatcher=dispatcher,
            credential_authority=authority,
            event_visibility_policy=visibility,
            state_store=state,
            server_generation=generation,
        )
        client: GatewayWebSocketClient | None = None
        try:
            await channels.start()
            await asyncio.wait_for(provider.ready.wait(), timeout=1)
            await gateway_server.start()
            await channels.activate()
            events.attach_live_sink(gateway_server.publish, loop=asyncio.get_running_loop())

            client = GatewayWebSocketClient(
                GatewayClientConfig(url=gateway_server.url, token=_GATEWAY_TOKEN)
            )
            hello = await client.connect()
            assert hello.scopes == ("gateway.read", "chat.write", "chat.abort")

            created = await client.request(
                "sessions.create",
                SessionsCreateParams(),
                idempotency_key="create-session",
            )
            assert isinstance(created, SessionsCreateResult)
            session_id = created.session.session_id
            subscription = client.subscribe(events=("chat.final",), session_id=session_id)
            accepted = await client.request(
                "chat.send",
                ChatSendParams(session_id, (TextRpcSegment("hello from acp"),)),
                idempotency_key="send-acp-turn",
            )
            final = await asyncio.wait_for(subscription.get(), timeout=1)
            assert isinstance(final.payload, ChatFinalEvent)
            assert final.payload.run_id == accepted.run_id
            assert final.payload.segments == (TextRpcSegment("gateway-answer"),)
            subscription.close()

            provider.emit_message.set()
            try:
                await asyncio.wait_for(provider.outbound_received.wait(), timeout=2)
            except asyncio.TimeoutError as exc:
                raise AssertionError(
                    f"QQ outbound was not observed; ingress_errors={ingress_errors!r}"
                ) from exc
            await _wait_for_completed_ingress(state)

            send_action = provider.actions[-1]
            assert send_action["params"] == {
                "message_type": "group",
                "group_id": _GROUP,
                "message": [
                    {"type": "reply", "data": {"id": "message-1"}},
                    {"type": "text", "data": {"text": "gateway-answer"}},
                ],
            }
            assert len(executor.requests) == 2
            qq_request = next(item for item in executor.requests if item.principal.channel == "qq")
            assert qq_request.principal.user_id == _ACTOR
            assert qq_request.principal.role is Role.OWNER
            assert qq_request.principal.conversation.chat_id == _GROUP

            receipt_stages = [
                record.payload["stage"]
                for record in state.events_after(0, limit=100)
                if record.event == "delivery.updated"
            ]
            assert receipt_stages == [
                "gateway_accepted",
                "provider_submitted",
                "provider_acknowledged",
            ]
            assert provider.errors == []
        finally:
            if client is not None:
                await client.close()
            await gateway_server.stop()
            await channels.stop()
            await coordinator.close()
            provider_server.close()
            await provider_server.wait_closed()

    asyncio.run(scenario())


async def _wait_for_completed_ingress(state: GatewayStateStore) -> None:
    for _ in range(100):
        completed = state.list_ingress(states=("completed",), limit=10)
        if completed:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("QQ ingress did not complete")


def _is_normal_connection_close(exc: BaseException) -> bool:
    exceptions = getattr(websockets, "exceptions", None)
    connection_closed = getattr(exceptions, "ConnectionClosed", ())
    return bool(connection_closed) and isinstance(exc, connection_closed)
