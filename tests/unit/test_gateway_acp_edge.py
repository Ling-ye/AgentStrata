from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Any

from acp.exceptions import RequestError
from acp.schema import ImageContentBlock, TextContentBlock
import pytest

from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.gateway_rpc import (
    ChatAbortParams,
    ChatAbortResult,
    ChatFinalEvent,
    ChatSendParams,
    ChatSendResult,
    ChatUpdateEvent,
    GatewayMethodResult,
    GatewayRequestParams,
    RunSnapshot,
    RunsGetParams,
    RunsGetResult,
    RunsLatestParams,
    RunsLatestResult,
    SessionSnapshot,
    SessionsCreateParams,
    SessionsCreateResult,
    SessionsGetParams,
    SessionsGetResult,
    SessionsListParams,
    SessionsListResult,
    SessionsPatchParams,
    SessionsPatchResult,
    ResourceRpcSegment,
    TextRpcSegment,
)
from chatcopilot.protocols.acp.server import GatewayAcpAgent, config_from_env
from chatcopilot.protocols.gateway_client import (
    GatewayConnectionClosed,
    GatewayEventSubscriptionProtocol,
    GatewayMutationOutcomeUnknown,
    GatewayRecoveryRequired,
    TypedGatewayEvent,
)


def _snapshot(
    session_id: str,
    *,
    mode: str = "default",
    active_run_id: str | None = None,
) -> SessionSnapshot:
    return SessionSnapshot(
        session_id=session_id,
        account=ChannelAccountRef(channel="acp", account_id="local-edge"),
        conversation=ConversationRef(kind="local", conversation_id="bound-client"),
        mode=mode,
        debug=False,
        event_cursor=0,
        active_run_id=active_run_id,
    )


def test_runtime_config_reads_only_the_loopback_gateway_credential() -> None:
    token = "g" * 48
    config = config_from_env(
        {
            "CHATCOPILOT_GATEWAY_URL": "ws://127.0.0.1:18789",
            "CHATCOPILOT_GATEWAY_TOKEN": token,
            "QQ_ACCESS_TOKEN": "q" * 48,
            "CHATCOPILOT_WORKSPACE_ROOT": "/must/not/be/read/by-edge",
        }
    )

    assert config.gateway.url == "ws://127.0.0.1:18789"
    assert config.gateway.token == token
    assert config.gateway.client_id == "acp-edge"
    assert "g" * 16 not in repr(config)


@pytest.mark.parametrize(
    "environ",
    (
        {"CHATCOPILOT_GATEWAY_TOKEN": "g" * 48},
        {"CHATCOPILOT_GATEWAY_URL": "ws://127.0.0.1:18789"},
    ),
)
def test_runtime_config_rejects_missing_gateway_boundary(environ: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        config_from_env(environ)


class FakeSubscription(GatewayEventSubscriptionProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[TypedGatewayEvent | Exception] = asyncio.Queue()
        self.closed = False

    async def get(self) -> TypedGatewayEvent:
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True

    def put(self, event: TypedGatewayEvent) -> None:
        self.queue.put_nowait(event)

    def fail(self, error: Exception) -> None:
        self.queue.put_nowait(error)


class FakeGatewayClient:
    def __init__(self) -> None:
        self.connected = True
        self.requests: list[tuple[str, GatewayRequestParams, str | None]] = []
        self.subscriptions: dict[str, FakeSubscription] = {}
        self.block_chat_send = False
        self.chat_send_started = asyncio.Event()
        self.release_chat_send = asyncio.Event()
        self.abort_requested = asyncio.Event()
        self.session_ids = ["session-a", "session-b"]
        self.run_states: dict[str, RunSnapshot] = {}
        self.active_run_ids: dict[str, str] = {}
        self.latest_runs: dict[str, RunSnapshot] = {}
        self.chat_send_failures: list[Exception] = []

    async def ensure_connected(self) -> None:
        self.connected = True

    async def request(
        self,
        method: str,
        params: GatewayRequestParams,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayMethodResult:
        self.requests.append((method, params, idempotency_key))
        if method == "sessions.create":
            assert isinstance(params, SessionsCreateParams)
            return SessionsCreateResult(_snapshot("session-a"))
        if method == "sessions.get":
            assert isinstance(params, SessionsGetParams)
            return SessionsGetResult(
                _snapshot(
                    params.session_id,
                    active_run_id=self.active_run_ids.get(params.session_id),
                )
            )
        if method == "sessions.list":
            assert isinstance(params, SessionsListParams)
            return SessionsListResult(
                sessions=tuple(_snapshot(value) for value in self.session_ids),
                next_cursor=2,
            )
        if method == "sessions.patch":
            assert isinstance(params, SessionsPatchParams)
            return SessionsPatchResult(_snapshot(params.session_id, mode=params.mode or "default"))
        if method == "chat.send":
            assert isinstance(params, ChatSendParams)
            self.chat_send_started.set()
            if self.block_chat_send:
                await self.release_chat_send.wait()
            run_id = f"run-{params.session_id}"
            self.run_states.setdefault(
                run_id,
                RunSnapshot(params.session_id, run_id, "running"),
            )
            if self.chat_send_failures:
                self.connected = False
                raise self.chat_send_failures.pop(0)
            return ChatSendResult(session_id=params.session_id, run_id=run_id)
        if method == "runs.get":
            assert isinstance(params, RunsGetParams)
            return RunsGetResult(self.run_states[params.run_id])
        if method == "runs.latest":
            assert isinstance(params, RunsLatestParams)
            return RunsLatestResult(self.latest_runs.get(params.session_id))
        if method == "chat.abort":
            assert isinstance(params, ChatAbortParams)
            self.abort_requested.set()
            return ChatAbortResult(
                session_id=params.session_id,
                run_id=params.run_id,
                aborted=True,
            )
        raise AssertionError(f"unexpected Gateway method: {method}")

    def subscribe(
        self,
        *,
        events: Collection[str],
        session_id: str | None = None,
        max_queue: int | None = None,
    ) -> FakeSubscription:
        del events, max_queue
        assert session_id is not None
        subscription = FakeSubscription()
        self.subscriptions[session_id] = subscription
        return subscription


class FakeAcpClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


async def _authenticated_agent(
    gateway: FakeGatewayClient,
) -> tuple[GatewayAcpAgent, FakeAcpClient]:
    agent = GatewayAcpAgent(gateway)
    acp_client = FakeAcpClient()
    agent.on_connect(acp_client)  # type: ignore[arg-type]
    await agent.authenticate("gateway-local")
    return agent, acp_client


async def _wait_for_subscription(gateway: FakeGatewayClient, session_id: str) -> FakeSubscription:
    for _ in range(100):
        subscription = gateway.subscriptions.get(session_id)
        if subscription is not None:
            return subscription
        await asyncio.sleep(0)
    raise AssertionError("prompt did not subscribe to Gateway events")


def test_initialize_is_honest_and_local_authentication_is_not_a_noop() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent = GatewayAcpAgent(gateway)
        initialized = await agent.initialize(1)
        capabilities = initialized.agent_capabilities
        assert capabilities is not None
        assert capabilities.load_session is True
        assert capabilities.prompt_capabilities is not None
        assert capabilities.prompt_capabilities.image is False
        assert capabilities.prompt_capabilities.audio is False
        assert capabilities.prompt_capabilities.embedded_context is False
        assert initialized.auth_methods is not None
        assert [item.id for item in initialized.auth_methods] == ["gateway-local"]

        with pytest.raises(RequestError) as unauthenticated:
            await agent.new_session(cwd="/ignored")
        assert unauthenticated.value.code == -32000
        with pytest.raises(RequestError) as wrong_method:
            await agent.authenticate("external-identity")
        assert wrong_method.value.data == {"code": "local_gateway_auth_required"}
        await agent.authenticate("gateway-local")
        created = await agent.new_session(cwd="/never-read")
        assert created.session_id == "session-a"
        method, params, key = gateway.requests[-1]
        assert method == "sessions.create"
        assert params == SessionsCreateParams()
        assert key is not None

    asyncio.run(scenario())


def test_session_control_ignores_cwd_and_rejects_external_directories_and_mcp() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, _client = await _authenticated_agent(gateway)
        loaded = await agent.load_session(cwd="file:///never-read", session_id="session-a")
        assert loaded.modes is not None
        assert loaded.modes.current_mode_id == "default"
        listed = await agent.list_sessions(cwd="/never-read", cursor="0")
        assert [item.session_id for item in listed.sessions] == ["session-a", "session-b"]
        assert all(item.cwd == "" for item in listed.sessions)
        assert listed.next_cursor == "2"
        await agent.set_session_mode("session-a", "debug")
        method, params, key = gateway.requests[-1]
        assert method == "sessions.patch"
        assert params == SessionsPatchParams(session_id="session-a", mode="debug")
        assert key is not None

        with pytest.raises(RequestError) as directories:
            await agent.new_session(cwd="/ignored", additional_directories=["/private"])
        assert directories.value.data == {"code": "gateway_controls_runtime_context"}
        with pytest.raises(RequestError) as mcp:
            await agent.new_session(cwd="/ignored", mcp_servers=[object()])  # type: ignore[list-item]
        assert mcp.value.data == {"code": "gateway_controls_runtime_context"}

    asyncio.run(scenario())


def test_prompt_accepts_only_text_and_never_reads_resource_paths_or_uris() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, _client = await _authenticated_agent(gateway)
        image = ImageContentBlock(
            type="image",
            data="not-decoded",
            mime_type="image/png",
            uri="file:///private/never-read.png",
        )
        with pytest.raises(RequestError) as raised:
            await agent.prompt(session_id="session-a", prompt=[image])
        assert raised.value.data == {"code": "text_prompt_only"}
        assert not any(method == "chat.send" for method, _params, _key in gateway.requests)

    asyncio.run(scenario())


def test_prompt_streams_only_its_exact_session_and_run_then_finishes() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, client = await _authenticated_agent(gateway)
        task = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="hello")],
            )
        )
        subscription = await _wait_for_subscription(gateway, "session-a")
        await gateway.chat_send_started.wait()
        await asyncio.sleep(0)
        subscription.put(
            TypedGatewayEvent(
                "chat.final",
                1,
                ChatFinalEvent(
                    session_id="session-a",
                    run_id="old-run",
                    stop_reason="completed",
                    segments=(TextRpcSegment("late"),),
                ),
            )
        )
        subscription.put(
            TypedGatewayEvent(
                "chat.update",
                2,
                ChatUpdateEvent(
                    session_id="session-a",
                    run_id="run-session-a",
                    text="answer",
                ),
            )
        )
        subscription.put(
            TypedGatewayEvent(
                "chat.final",
                3,
                ChatFinalEvent(
                    session_id="session-a",
                    run_id="run-session-a",
                    stop_reason="completed",
                ),
            )
        )
        response = await asyncio.wait_for(task, timeout=1.0)
        assert response.stop_reason == "end_turn"
        assert [(sid, update.content.text) for sid, update in client.updates] == [
            ("session-a", "answer")
        ]
        assert subscription.closed
        method, params, _key = next(row for row in gateway.requests if row[0] == "chat.send")
        assert method == "chat.send"
        assert params == ChatSendParams(
            session_id="session-a",
            segments=(TextRpcSegment("hello"),),
        )

    asyncio.run(scenario())


def test_concurrent_session_events_are_isolated() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, client = await _authenticated_agent(gateway)
        first = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="first")],
            )
        )
        second = asyncio.create_task(
            agent.prompt(
                session_id="session-b",
                prompt=[TextContentBlock(type="text", text="second")],
            )
        )
        sub_a, sub_b = await asyncio.gather(
            _wait_for_subscription(gateway, "session-a"),
            _wait_for_subscription(gateway, "session-b"),
        )
        await asyncio.sleep(0)
        sub_b.put(
            TypedGatewayEvent(
                "chat.update",
                1,
                ChatUpdateEvent("session-b", "run-session-b", "B"),
            )
        )
        sub_a.put(
            TypedGatewayEvent(
                "chat.update",
                2,
                ChatUpdateEvent("session-a", "run-session-a", "A"),
            )
        )
        sub_b.put(
            TypedGatewayEvent(
                "chat.final",
                3,
                ChatFinalEvent("session-b", "run-session-b", "completed"),
            )
        )
        sub_a.put(
            TypedGatewayEvent(
                "chat.final",
                4,
                ChatFinalEvent("session-a", "run-session-a", "completed"),
            )
        )
        responses = await asyncio.gather(first, second)
        assert [response.stop_reason for response in responses] == ["end_turn", "end_turn"]
        assert {(sid, update.content.text) for sid, update in client.updates} == {
            ("session-a", "A"),
            ("session-b", "B"),
        }

    asyncio.run(scenario())


def test_cancel_before_chat_acceptance_aborts_exact_run_and_waits_for_terminal() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        gateway.block_chat_send = True
        agent, _client = await _authenticated_agent(gateway)
        prompt_task = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="slow")],
            )
        )
        subscription = await _wait_for_subscription(gateway, "session-a")
        await gateway.chat_send_started.wait()
        await agent.cancel("session-a")
        assert not gateway.abort_requested.is_set()
        gateway.release_chat_send.set()
        await asyncio.wait_for(gateway.abort_requested.wait(), timeout=1.0)
        abort = next(params for method, params, _key in gateway.requests if method == "chat.abort")
        assert abort == ChatAbortParams(
            session_id="session-a",
            run_id="run-session-a",
        )
        subscription.put(
            TypedGatewayEvent(
                "chat.final",
                1,
                ChatFinalEvent("session-a", "run-session-a", "aborted"),
            )
        )
        response = await asyncio.wait_for(prompt_task, timeout=1.0)
        assert response.stop_reason == "cancelled"

    asyncio.run(scenario())


def test_cancel_racing_with_late_completed_final_does_not_claim_cancelled() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, _client = await _authenticated_agent(gateway)
        prompt_task = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="race")],
            )
        )
        subscription = await _wait_for_subscription(gateway, "session-a")
        await gateway.chat_send_started.wait()
        await asyncio.sleep(0)
        await agent.cancel("session-a")
        await asyncio.wait_for(gateway.abort_requested.wait(), timeout=1.0)
        subscription.put(
            TypedGatewayEvent(
                "chat.final",
                1,
                ChatFinalEvent("session-a", "run-session-a", "completed"),
            )
        )
        response = await asyncio.wait_for(prompt_task, timeout=1.0)
        assert response.stop_reason == "end_turn"

    asyncio.run(scenario())


def test_connection_drop_interrupts_prompt_with_stable_gateway_error() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, _client = await _authenticated_agent(gateway)
        prompt_task = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="drop")],
            )
        )
        subscription = await _wait_for_subscription(gateway, "session-a")
        await gateway.chat_send_started.wait()
        await asyncio.sleep(0)
        subscription.fail(
            GatewayConnectionClosed(
                "gateway_connection_lost",
                "private transport detail must not escape",
            )
        )
        with pytest.raises(RequestError) as raised:
            await asyncio.wait_for(prompt_task, timeout=1.0)
        assert str(raised.value) == "Gateway unavailable"
        assert raised.value.data == {"code": "gateway_connection_lost"}

    asyncio.run(scenario())


def test_unknown_chat_outcome_keeps_original_key_and_reconciles_before_new_prompt() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        gateway.chat_send_failures.append(
            GatewayMutationOutcomeUnknown(
                "mutation_outcome_unknown",
                "private transport detail must not escape",
            )
        )
        agent, client = await _authenticated_agent(gateway)

        with pytest.raises(RequestError) as unknown:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="original")],
            )
        assert unknown.value.data == {"code": "mutation_outcome_unknown"}
        first_send = next(row for row in gateway.requests if row[0] == "chat.send")
        original_key = first_send[2]
        assert original_key is not None

        gateway.run_states["run-session-a"] = RunSnapshot(
            "session-a",
            "run-session-a",
            "completed",
            segments=(TextRpcSegment("recovered answer"),),
        )
        with pytest.raises(RequestError) as recovered:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="must not be submitted")],
            )
        assert recovered.value.data == {"code": "previous_turn_recovered_retry_prompt"}

        sends = [row for row in gateway.requests if row[0] == "chat.send"]
        assert len(sends) == 2
        assert sends[0][1:] == sends[1][1:]
        assert sends[1][1] == ChatSendParams(
            session_id="session-a",
            segments=(TextRpcSegment("original"),),
        )
        assert sends[1][2] == original_key
        assert [(sid, update.content.text) for sid, update in client.updates] == [
            ("session-a", "recovered answer")
        ]

    asyncio.run(scenario())


def test_restarted_acp_recovers_latest_terminal_run_before_accepting_new_text() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        recovered = RunSnapshot(
            "session-a",
            "run-before-restart",
            "completed",
            segments=(TextRpcSegment("durable answer"),),
        )
        gateway.latest_runs["session-a"] = recovered
        gateway.run_states[recovered.run_id] = recovered
        agent, client = await _authenticated_agent(gateway)

        with pytest.raises(RequestError) as raised:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="new text must wait")],
            )

        assert raised.value.data == {"code": "previous_turn_recovered_retry_prompt"}
        assert not any(method == "chat.send" for method, _params, _key in gateway.requests)
        assert [(sid, update.content.text) for sid, update in client.updates] == [
            ("session-a", "durable answer")
        ]
        assert [method for method, _params, _key in gateway.requests] == [
            "sessions.get",
            "runs.latest",
            "runs.get",
        ]

    asyncio.run(scenario())


def test_restarted_acp_queries_active_run_without_resubmitting_any_prompt() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        run_id = "run-before-restart"
        gateway.active_run_ids["session-a"] = run_id
        gateway.run_states[run_id] = RunSnapshot("session-a", run_id, "running")
        agent, client = await _authenticated_agent(gateway)

        with pytest.raises(RequestError) as active:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="must not become old input")],
            )
        assert active.value.data == {"code": "previous_turn_still_active"}
        assert not any(method == "chat.send" for method, _params, _key in gateway.requests)

        gateway.active_run_ids.pop("session-a")
        gateway.run_states[run_id] = RunSnapshot(
            "session-a",
            run_id,
            "completed",
            segments=(TextRpcSegment("finished after restart"),),
        )
        with pytest.raises(RequestError) as terminal:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="still must not be submitted")],
            )
        assert terminal.value.data == {"code": "previous_turn_recovered_retry_prompt"}
        assert not any(method == "chat.send" for method, _params, _key in gateway.requests)
        assert [(sid, update.content.text) for sid, update in client.updates] == [
            ("session-a", "finished after restart")
        ]

    asyncio.run(scenario())


def test_restarted_acp_projects_failed_terminal_without_submitting_new_text() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        failed = RunSnapshot(
            "session-a",
            "run-before-restart",
            "failed",
            error_code="model_failed",
        )
        gateway.latest_runs["session-a"] = failed
        gateway.run_states[failed.run_id] = failed
        agent, _client = await _authenticated_agent(gateway)

        with pytest.raises(RequestError) as raised:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="must not be submitted")],
            )

        assert raised.value.data == {"code": "model_failed", "retryable": False}
        assert not any(method == "chat.send" for method, _params, _key in gateway.requests)

    asyncio.run(scenario())


def test_restarted_acp_fails_closed_when_active_run_binding_drifts() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        gateway.active_run_ids["session-a"] = "run-before-restart"
        gateway.run_states["run-before-restart"] = RunSnapshot(
            "session-a",
            "different-run",
            "running",
        )
        agent, _client = await _authenticated_agent(gateway)

        with pytest.raises(RequestError) as raised:
            await agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="must not be submitted")],
            )

        assert raised.value.data == {"code": "invalid_run_result"}
        assert not any(method == "chat.send" for method, _params, _key in gateway.requests)

    asyncio.run(scenario())


def test_event_resync_interrupts_prompt_instead_of_claiming_a_terminal_result() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, _client = await _authenticated_agent(gateway)
        prompt_task = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="recover")],
            )
        )
        subscription = await _wait_for_subscription(gateway, "session-a")
        await gateway.chat_send_started.wait()
        await asyncio.sleep(0)
        subscription.fail(
            GatewayRecoveryRequired(
                "event_resync_required",
                "private recovery detail must not escape",
            )
        )
        with pytest.raises(RequestError) as raised:
            await asyncio.wait_for(prompt_task, timeout=1.0)
        assert str(raised.value) == "Gateway event recovery required"
        assert raised.value.data == {"code": "event_resync_required"}

    asyncio.run(scenario())


def test_non_text_gateway_output_is_rejected_instead_of_opening_a_resource() -> None:
    async def scenario() -> None:
        gateway = FakeGatewayClient()
        agent, _client = await _authenticated_agent(gateway)
        prompt_task = asyncio.create_task(
            agent.prompt(
                session_id="session-a",
                prompt=[TextContentBlock(type="text", text="file")],
            )
        )
        subscription = await _wait_for_subscription(gateway, "session-a")
        await gateway.chat_send_started.wait()
        await asyncio.sleep(0)
        subscription.put(
            TypedGatewayEvent(
                "chat.final",
                1,
                ChatFinalEvent(
                    "session-a",
                    "run-session-a",
                    "completed",
                    segments=(ResourceRpcSegment(kind="file", resource_id="opaque-file"),),
                ),
            )
        )
        with pytest.raises(RequestError) as raised:
            await asyncio.wait_for(prompt_task, timeout=1.0)
        assert raised.value.data == {"code": "unsupported_gateway_output"}

    asyncio.run(scenario())
