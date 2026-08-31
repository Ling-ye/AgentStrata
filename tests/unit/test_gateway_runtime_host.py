from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chatcopilot.botspec.model import (
    ChannelsSpec,
    GatewaySpec,
    QQChannelSpec,
    WikiSpec,
    WorkspaceSpec,
)
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.channels.base import ChannelHealth
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.gateway_protocol import RequestFrame
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.core.config import ChatConfig
from chatcopilot.gateway import runtime as runtime_module
from chatcopilot.gateway.channels import ChannelRuntimeError
from chatcopilot.gateway.coordinator import GatewayTurnCoordinatorError
from chatcopilot.gateway.runtime import (
    GatewayRuntimeConfigurationError,
    GatewayRuntimeLifecycleError,
    build_gateway_runtime_host,
    parse_gateway_runtime_config,
    serve_gateway_runtime,
)
from chatcopilot.gateway.server import (
    GatewayClientContext,
    GatewayDispatchError,
    GatewayServerHealth,
)
from chatcopilot.gateway.state_store import GatewayStateStore


def _principal(event: CanonicalInboundEvent) -> Principal:
    evidence = event.evidence
    return Principal(
        channel=evidence.account.channel,
        account_id=evidence.account.account_id,
        conversation=ConversationIdentity(
            platform=evidence.account.channel,
            chat_kind=evidence.conversation.kind,
            chat_id=evidence.conversation.conversation_id,
        ),
        user_id=evidence.sender.sender_id,
        role=Role.USER,
        evidence_digest="sha256:" + evidence.frame_sha256,
    )


def _runtime(*, wiki_enabled: bool = True) -> BotRuntimeContext:
    gateway = GatewaySpec()
    channels = ChannelsSpec(qq=QQChannelSpec())
    spec = SimpleNamespace(
        workspace=WorkspaceSpec(),
        context=SimpleNamespace(wiki=WikiSpec(enabled=wiki_enabled)),
        llm=SimpleNamespace(env_prefix="CHATCOPILOT_TEST"),
    )
    return cast(
        BotRuntimeContext,
        SimpleNamespace(
            gateway=gateway,
            channels=channels,
            spec=spec,
            bot_id="test-qq",
            instance_id="test-qq",
        ),
    )


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _environment(tmp_path: Path) -> dict[str, str]:
    state_anchor = _private_dir(tmp_path / "state")
    workspace = _private_dir(tmp_path / "workspace")
    wiki = _private_dir(tmp_path / "wiki")
    return {
        "CHATCOPILOT_GATEWAY_PORT": "18789",
        "CHATCOPILOT_GATEWAY_TOKEN": "g" * 48,
        "CHATCOPILOT_GATEWAY_STATE_ROOT": str(state_anchor / "gateway"),
        "CHATCOPILOT_WORKSPACE_ROOT": str(workspace),
        "CHATCOPILOT_WIKI_ROOT": str(wiki),
        "CHATCOPILOT_QQ_ONEBOT_WS_URL": "ws://127.0.0.1:3001",
        "QQ_ACCESS_TOKEN": "q" * 48,
        "QQ_ACCOUNT": "10001",
        "QQ_ALLOW_FROM": "10002",
        "QQ_ALLOW_GROUPS": "20001",
    }


def test_runtime_config_is_strict_and_redacts_both_credentials(tmp_path: Path) -> None:
    config = parse_gateway_runtime_config(_runtime(), _environment(tmp_path))

    assert config.host == "127.0.0.1"
    assert config.port == 18789
    assert config.state_root.name == "gateway"
    assert "g" * 16 not in repr(config)
    assert "q" * 16 not in repr(config)


@pytest.mark.parametrize(
    "legacy_key",
    (
        "QQ_WS_URL",
        "QQ_AT_PROXY_URL",
        "QQ_REQUIRE_AT_IN_GROUP",
        "QQ_AT_ALL_COUNTS",
        "CHATCOPILOT_CC_CONNECT_BIN",
        "CHATCOPILOT_CC_HOME",
        "CHATCOPILOT_CC_CONNECT_CONFIG_DIR",
        "CHATCOPILOT_SESSION_ENV_DIR",
    ),
)
def test_runtime_config_rejects_every_legacy_qq_bridge_key(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    environ = _environment(tmp_path)
    environ[legacy_key] = ""

    with pytest.raises(GatewayRuntimeConfigurationError) as caught:
        parse_gateway_runtime_config(_runtime(), environ)

    assert caught.value.code == "legacy_qq_runtime_env_removed"


def test_runtime_config_rejects_shared_gateway_and_onebot_token(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    environ["CHATCOPILOT_GATEWAY_TOKEN"] = environ["QQ_ACCESS_TOKEN"]

    with pytest.raises(GatewayRuntimeConfigurationError) as caught:
        parse_gateway_runtime_config(_runtime(), environ)

    assert caught.value.code == "gateway_token_reused"


class _FakeAgentRuntime:
    def __init__(self, lifecycle: list[str]) -> None:
        self.lifecycle = lifecycle

    def close(self) -> None:
        self.lifecycle.append("agent.close")


class _FakeActorFactory:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args
        self.session_manager = kwargs["session_manager"]
        self.lifecycle = _LIFECYCLE

    def close(self) -> tuple[()]:
        self.lifecycle.append("actors.close")
        return ()


class _FakeDriver:
    def __init__(self, config: Any, on_event: Any) -> None:
        self.channel_id = config.channel_id
        self.account = ChannelAccountRef("qq", config.account_id)
        self.on_event = on_event
        self.state = "stopped"
        self.connection_generation: str | None = None

    async def start(self) -> None:
        _LIFECYCLE.append("channel.start")
        self.state = "ready"
        self.connection_generation = "connection-test"

    async def stop(self) -> None:
        _LIFECYCLE.append("channel.stop")
        self.state = "stopped"
        self.connection_generation = None

    def health(self) -> ChannelHealth:
        return ChannelHealth(
            channel_id=self.channel_id,
            account=self.account,
            state=cast(Any, self.state),
            connection_generation=self.connection_generation,
        )

    async def send(self, envelope: Any) -> Any:
        raise AssertionError(f"unexpected outbound: {envelope!r}")


class _FakeServer:
    def __init__(self, **kwargs: Any) -> None:
        self.config = kwargs["config"]
        self.dispatcher = kwargs["dispatcher"]
        self.state_store = kwargs["state_store"]
        self.generation = kwargs["server_generation"]
        self.running = False
        self.fail_start = False
        self.start_entered: asyncio.Event | None = None
        self.start_release: asyncio.Event | None = None

    async def start(self) -> None:
        _LIFECYCLE.append("server.start")
        self.running = True
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.fail_start:
            raise RuntimeError("synthetic server failure")

    async def stop(self) -> None:
        _LIFECYCLE.append("server.stop")
        self.running = False

    def publish(self, frame: Any) -> None:
        del frame

    def health(self) -> GatewayServerHealth:
        return GatewayServerHealth(
            running=self.running,
            accepting=self.running,
            ready=self.running
            and self.state_store.current_writer_generation() == self.generation,
            host=self.config.host,
            port=self.config.port if self.running else None,
            server_generation=self.generation,
            current_generation=self.state_store.current_writer_generation(),
            active_connections=0,
            inflight_requests=0,
            event_cursor=0,
        )


_LIFECYCLE: list[str] = []


def _build_fake_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[Any, _FakeAgentRuntime]:
    _LIFECYCLE.clear()
    agent = _FakeAgentRuntime(_LIFECYCLE)
    monkeypatch.setattr(runtime_module, "load_config", lambda **_: ChatConfig())
    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", lambda *_, **__: agent)
    monkeypatch.setattr(runtime_module, "ActorSessionFactory", _FakeActorFactory)
    monkeypatch.setattr(runtime_module, "OneBotForwardWebSocketDriver", _FakeDriver)
    monkeypatch.setattr(runtime_module, "GatewayWebSocketServer", _FakeServer)
    host = build_gateway_runtime_host(
        _runtime(),
        environ=_environment(tmp_path) if environ is None else environ,
    )
    return host, agent


def test_host_starts_with_one_shared_generation_and_fences_channel_before_listener_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host, _ = _build_fake_host(monkeypatch, tmp_path)

    async def exercise() -> None:
        await host.start()

        assert host.ready
        assert host.generation == 1
        assert host.state_store.current_writer_generation() == 1
        assert host.session_manager.writer_generation == 1
        assert host.channels.health().writer_generation == 1
        assert host.server.generation == 1
        assert _LIFECYCLE == ["channel.start", "server.start"]

        await host.stop()

    asyncio.run(exercise())

    assert _LIFECYCLE == [
        "channel.start",
        "server.start",
        "channel.stop",
        "server.stop",
        "actors.close",
        "agent.close",
    ]
    assert not host.ready


def test_duplicate_host_fails_before_generation_change_and_releases_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    host, _ = _build_fake_host(monkeypatch, tmp_path, environ=environ)
    assert host.generation == 1

    with pytest.raises(GatewayRuntimeLifecycleError) as caught:
        build_gateway_runtime_host(_runtime(), environ=environ)

    assert caught.value.code == "gateway_instance_already_running"
    assert host.state_store.current_writer_generation() == 1
    assert _LIFECYCLE == []

    asyncio.run(host.stop())
    replacement, _ = _build_fake_host(monkeypatch, tmp_path, environ=environ)
    assert replacement.generation == 2
    asyncio.run(replacement.stop())


def test_production_composition_persists_allowed_and_denied_admission_decisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host, _ = _build_fake_host(monkeypatch, tmp_path)

    def inbound(sender_id: str) -> CanonicalInboundEvent:
        return CanonicalInboundEvent(
            evidence=TransportEvidence(
                account=ChannelAccountRef("qq", "10001"),
                conversation=ConversationRef("p2p", sender_id),
                sender=SenderClaim(sender_id),
                event_id=f"event-{sender_id}",
                message_id=f"message-{sender_id}",
                connection_generation="connection-test",
                frame_sha256="a" * 64,
                observed_at=1.0,
            ),
            segments=(MessageSegment(kind="text", text="hello"),),
        )

    async def exercise() -> None:
        await host.start()
        host.coordinator.authorize_inbound(inbound("10002"))
        with pytest.raises(GatewayTurnCoordinatorError) as denied:
            host.coordinator.authorize_inbound(inbound("99999"))
        assert denied.value.code == "qq-private-user-not-allowed"
        await host.stop()

    asyncio.run(exercise())

    records = host.state_store.list_authorization_decisions()
    assert len(records) == 2
    assert {record.decision.code for record in records} == {
        "qq-private-user-allowed",
        "qq-private-user-not-allowed",
    }
    assert {record.decision.allowed for record in records} == {True, False}


def test_server_start_failure_rolls_back_channel_and_internal_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host, _ = _build_fake_host(monkeypatch, tmp_path)
    host.server.fail_start = True

    with pytest.raises(GatewayRuntimeLifecycleError) as caught:
        asyncio.run(host.start())

    assert caught.value.code == "gateway_start_failed"
    assert _LIFECYCLE == [
        "channel.start",
        "server.start",
        "channel.stop",
        "server.stop",
        "actors.close",
        "agent.close",
    ]
    assert not host.ready


def test_cancelled_start_rollback_still_releases_instance_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    host, _ = _build_fake_host(monkeypatch, tmp_path, environ=environ)
    host.server.fail_start = True

    async def cancelled_channel_stop() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(host.channels, "stop", cancelled_channel_stop)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(host.start())

    state_root = Path(environ["CHATCOPILOT_GATEWAY_STATE_ROOT"])
    store = GatewayStateStore(state_root, trusted_anchor=state_root.parent)
    lease = store.acquire_instance_lease()
    lease.close()
    assert host.health().state == "failed"
    assert _LIFECYCLE == [
        "channel.start",
        "server.start",
        "actors.close",
        "agent.close",
    ]


def test_server_bind_failure_keeps_inbound_and_mutations_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host, _ = _build_fake_host(monkeypatch, tmp_path)
    host.server.fail_start = True

    async def exercise() -> None:
        host.server.start_entered = asyncio.Event()
        host.server.start_release = asyncio.Event()
        starting = asyncio.create_task(host.start())
        await host.server.start_entered.wait()
        assert host.channels.health().state == "prepared"

        inbound = asyncio.create_task(
            host.channels.handle_inbound(
                CanonicalInboundEvent(
                    evidence=TransportEvidence(
                        account=ChannelAccountRef("qq", "10001"),
                        conversation=ConversationRef("p2p", "10002"),
                        sender=SenderClaim("10002"),
                        event_id="event-during-bind",
                        message_id="message-during-bind",
                        connection_generation="connection-test",
                        frame_sha256="a" * 64,
                        observed_at=1.0,
                    ),
                    segments=(
                        MessageSegment(kind="text", text="must remain fenced"),
                    ),
                )
            )
        )
        await asyncio.sleep(0)
        assert not inbound.done()

        client = GatewayClientContext(
            client_id="acp-edge",
            client_version="test",
            client_mode="acp",
            protocol=1,
            scopes=("gateway.read", "chat.write", "chat.abort"),
            capabilities=(),
        )
        with pytest.raises(GatewayDispatchError) as mutation:
            await host.server.dispatcher.dispatch(
                RequestFrame(
                    request_id="create-before-ready",
                    method="sessions.create",
                    params={},
                    idempotency_key="create-before-ready",
                ),
                client=client,
            )
        assert mutation.value.code == "gateway_not_ready"
        assert host.state_store.list_sessions() == ()
        assert host.state_store.list_active_runs() == ()
        assert host.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-during-bind",
        ) is None

        host.server.start_release.set()
        with pytest.raises(GatewayRuntimeLifecycleError) as startup:
            await starting
        assert startup.value.code == "gateway_start_failed"
        with pytest.raises(ChannelRuntimeError) as stopped:
            await asyncio.wait_for(inbound, timeout=1)
        assert stopped.value.code == "channel_runtime_not_ready"
        assert host.state_store.list_sessions() == ()
        assert host.state_store.list_active_runs() == ()
        assert host.state_store.get_ingress(
            channel="qq",
            account_id="10001",
            event_id="event-during-bind",
        ) is None

    asyncio.run(exercise())


def test_agent_assembly_failure_does_not_fence_existing_writer_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    state_root = Path(environ["CHATCOPILOT_GATEWAY_STATE_ROOT"])
    existing = GatewayStateStore(state_root, trusted_anchor=state_root.parent)
    assert existing.acquire_writer_generation(now=1.0) == 1
    monkeypatch.setattr(runtime_module, "load_config", lambda **_: ChatConfig())

    def fail_assembly(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("synthetic assembly failure")

    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", fail_assembly)

    with pytest.raises(RuntimeError, match="synthetic assembly failure"):
        build_gateway_runtime_host(_runtime(), environ=environ)

    assert existing.current_writer_generation() == 1
    lease = existing.acquire_instance_lease()
    lease.close()


def test_build_failure_after_generation_releases_instance_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    agent = _FakeAgentRuntime(_LIFECYCLE)
    _LIFECYCLE.clear()
    monkeypatch.setattr(runtime_module, "load_config", lambda **_: ChatConfig())
    monkeypatch.setattr(
        runtime_module,
        "assemble_agent_runtime",
        lambda *_, **__: agent,
    )

    class _FailingActorFactory:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("synthetic actor factory failure")

    monkeypatch.setattr(runtime_module, "ActorSessionFactory", _FailingActorFactory)

    with pytest.raises(RuntimeError, match="synthetic actor factory failure"):
        build_gateway_runtime_host(_runtime(), environ=environ)

    state_root = Path(environ["CHATCOPILOT_GATEWAY_STATE_ROOT"])
    store = GatewayStateStore(state_root, trusted_anchor=state_root.parent)
    assert store.current_writer_generation() == 1
    lease = store.acquire_instance_lease()
    lease.close()
    assert _LIFECYCLE == ["agent.close"]


def test_build_hydrates_then_closes_recovery_required_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    state_root = Path(environ["CHATCOPILOT_GATEWAY_STATE_ROOT"])
    first = GatewayStateStore(state_root, trusted_anchor=state_root.parent)
    first_generation = first.acquire_writer_generation(now=1.0)
    first.create_session(
        generation=first_generation,
        session_id="session-before-restart",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("p2p", "10002"),
        now=2.0,
    )
    first.begin_run(
        generation=first_generation,
        session_id="session-before-restart",
        run_id="run-before-restart",
        input_fingerprint="a" * 64,
        now=3.0,
    )
    first.start_run(
        generation=first_generation,
        session_id="session-before-restart",
        run_id="run-before-restart",
        now=4.0,
    )
    interrupted_ingress = CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=ChannelAccountRef("qq", "10001"),
            conversation=ConversationRef("p2p", "10002"),
            sender=SenderClaim("10002"),
            event_id="event-before-restart",
            message_id="message-before-restart",
            connection_generation="connection-before-restart",
            frame_sha256="b" * 64,
            observed_at=5.0,
        ),
        segments=(MessageSegment(kind="text", text="interrupted"),),
    )
    first.reserve_ingress(
        generation=first_generation,
        event=interrupted_ingress,
        principal=_principal(interrupted_ingress),
    )
    assert first.claim_ingress(
        generation=first_generation,
        channel="qq",
        account_id="10001",
        event_id="event-before-restart",
        now=6.0,
    )

    _LIFECYCLE.clear()
    agent = _FakeAgentRuntime(_LIFECYCLE)
    monkeypatch.setattr(runtime_module, "load_config", lambda **_: ChatConfig())
    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", lambda *_, **__: agent)
    monkeypatch.setattr(runtime_module, "ActorSessionFactory", _FakeActorFactory)
    monkeypatch.setattr(runtime_module, "OneBotForwardWebSocketDriver", _FakeDriver)
    monkeypatch.setattr(runtime_module, "GatewayWebSocketServer", _FakeServer)

    host = build_gateway_runtime_host(_runtime(), environ=environ)

    durable_run = host.state_store.get_run("run-before-restart")
    durable_session = host.state_store.get_session("session-before-restart")
    local_session = host.session_manager.get_session("session-before-restart")
    assert host.generation == 2
    assert durable_run is not None
    assert durable_run.state == "failed"
    assert durable_run.error_code == "gateway_restart_recovery"
    assert durable_session is not None and durable_session.active_run_id is None
    assert local_session.active_run_id is None
    assert host.state_store.events_after(0)[-1].event == "chat.error"
    recovered_ingress = host.state_store.get_ingress(
        channel="qq",
        account_id="10001",
        event_id="event-before-restart",
    )
    assert recovered_ingress is not None
    assert recovered_ingress.state == "failed"

    asyncio.run(host.stop())


def test_injected_shutdown_event_runs_clean_start_and_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host, _ = _build_fake_host(monkeypatch, tmp_path)
    async def exercise() -> None:
        stop_event = asyncio.Event()
        stop_event.set()
        await serve_gateway_runtime(host, stop_event=stop_event)

    asyncio.run(exercise())

    assert _LIFECYCLE[-4:] == [
        "channel.stop",
        "server.stop",
        "actors.close",
        "agent.close",
    ]
