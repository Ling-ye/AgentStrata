"""Client-only composition uses the real authenticated Gateway, without platform drivers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from chatcopilot.botspec.channel_configuration import configuration_adapter
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import ChannelsSpec, GatewayClientSpec, GatewaySpec
from chatcopilot.botspec.runtime import assemble_runtime_context
from chatcopilot.contracts.gateway_rpc import (
    ChatSendParams, ChatSendResult, RunsGetParams, RunsGetResult,
    SessionsCreateParams, SessionsCreateResult, SessionsGetParams, TextRpcSegment,
)
from chatcopilot.contracts.identity import Role
from chatcopilot.core.config import ChatConfig
from chatcopilot.gateway import runtime as runtime_module
from chatcopilot.gateway.runtime import build_gateway_runtime_host, parse_gateway_runtime_config
from chatcopilot.protocols.gateway_client import (
    GatewayClientConfig, GatewayClientError, GatewayRemoteError, GatewayWebSocketClient,
)
from tests.unit.test_botspec_gateway_channels import _BASE, _write_spec
from tests.unit.test_gateway_application_dispatcher import _ImmediateExecutor
from tests.unit.test_gateway_runtime_host import _environment, _runtime


def _client_data():
    data = deepcopy(_BASE)
    data.pop("channels")
    data["gateway"]["clients"] = [{"id": "avatar-owner", "token_env": "AVATAR_TOKEN", "role": "owner"}]
    return data


def test_client_spec_assembles_without_legacy_platform(tmp_path):
    spec = load_botspec(_write_spec(tmp_path, _client_data()))
    assert not [issue for issue in validate_botspec(spec) if issue.level == "error"]
    runtime = assemble_runtime_context(spec)
    assert runtime.platform_type == "gateway"
    assert runtime.channels == ChannelsSpec()
    assert runtime.gateway.clients == (GatewayClientSpec("avatar-owner", "AVATAR_TOKEN", "owner"),)
    adapter = configuration_adapter(spec)
    assert {field.env_key for field in adapter.required_secrets()} == {
        "AVATAR_TOKEN", "CHATCOPILOT_GATEWAY_PORT", "CHATCOPILOT_GATEWAY_STATE_ROOT"}
    generated = adapter.materialize_host_generated_secret("AVATAR_TOKEN", "")
    assert adapter.validate_runtime_env({"AVATAR_TOKEN": generated}) == ()


@pytest.mark.parametrize("change", [
    {"id": "acp-edge"}, {"id": "bad:workspace"}, {"role": "admin"}, {"token_env": "actual-secret"},
])
def test_client_identity_is_validated_before_runtime(tmp_path, change):
    data = _client_data()
    data["gateway"]["clients"][0].update(change)
    spec = load_botspec(_write_spec(tmp_path, data))
    assert any(issue.field.startswith("gateway.clients") for issue in validate_botspec(spec))


@pytest.mark.parametrize("value", [{}, ["owner"], [{"id": "owner", "token_env": "TOKEN", "scopes": ["gateway.admin"]}]])
def test_client_schema_rejects_ambiguous_authority(tmp_path, value):
    data = _client_data()
    data["gateway"]["clients"] = value
    with pytest.raises(ValueError, match="gateway.clients"):
        load_botspec(_write_spec(tmp_path, data))


def test_duplicate_client_ids_and_credential_references_are_rejected(tmp_path):
    data = _client_data()
    data["gateway"]["clients"] *= 2
    spec = load_botspec(_write_spec(tmp_path, data))
    errors = [issue.field for issue in validate_botspec(spec)]
    assert "gateway.clients[1].id" in errors
    assert "gateway.clients[1].token_env" in errors


def test_client_only_runtime_never_requires_platform_or_acp_credentials(tmp_path):
    runtime = _runtime(wiki_enabled=False)
    runtime.gateway = GatewaySpec(clients=(GatewayClientSpec("avatar-owner", "AVATAR_TOKEN", "owner"),))
    runtime.channels = ChannelsSpec()
    environment = _environment(tmp_path)
    environment = {key: value for key, value in environment.items()
                   if not key.startswith("QQ_") and key not in {
                       "CHATCOPILOT_GATEWAY_TOKEN", "CHATCOPILOT_QQ_ONEBOT_WS_URL"}}
    environment["AVATAR_TOKEN"] = "a" * 48
    config = parse_gateway_runtime_config(runtime, environment)
    assert config.channel is None and config.gateway_token is None
    assert config.client_roles == {"avatar-owner": Role.OWNER}
    assert config.credentials[0].client_mode == "interactive"
    assert "a" * 20 not in repr(config)


def test_client_host_authentication_sessions_execution_and_shutdown(tmp_path, monkeypatch):
    runtime = _runtime(wiki_enabled=False)
    runtime.gateway = GatewaySpec(clients=(
        GatewayClientSpec("avatar-owner", "AVATAR_TOKEN", "owner"),
        GatewayClientSpec("other-client", "OTHER_TOKEN"),
    ))
    runtime.channels = ChannelsSpec()
    environment = _environment(tmp_path)
    environment.update(AVATAR_TOKEN="a" * 48, OTHER_TOKEN="b" * 48)
    monkeypatch.setattr(runtime_module, "load_config", lambda **_: ChatConfig())
    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", lambda *_, **__: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runtime_module, "ActorSessionFactory", lambda **_: SimpleNamespace(close=lambda: None))
    executor = _ImmediateExecutor()
    monkeypatch.setattr(runtime_module, "ActorTurnExecutor", lambda *_, **__: executor)
    original = runtime_module.GatewayServerConfig
    monkeypatch.setattr(runtime_module, "GatewayServerConfig", lambda **kw: original(**{**kw, "port": 0}))

    async def exercise():
        host = build_gateway_runtime_host(runtime, environ=environment)
        await host.start()
        assert host.ready and host.health().channels.channels == ()
        url = f"ws://127.0.0.1:{host.server.health().port}"
        owner = GatewayWebSocketClient(GatewayClientConfig(url, "a" * 48,
            client_id="avatar-owner", client_mode="interactive"))
        other = GatewayWebSocketClient(GatewayClientConfig(url, "b" * 48,
            client_id="other-client", client_mode="interactive"))
        invalid = GatewayWebSocketClient(GatewayClientConfig(url, "z" * 48,
            client_id="avatar-owner", client_mode="interactive"))
        try:
            with pytest.raises(GatewayClientError):
                await invalid.connect()
            await owner.connect()
            await other.connect()
            created = await owner.request("sessions.create", SessionsCreateParams("avatar-history"),
                idempotency_key="create-avatar-history")
            assert isinstance(created, SessionsCreateResult)
            session_id = created.session.session_id
            with pytest.raises(GatewayRemoteError) as caught:
                await other.request("sessions.get", SessionsGetParams(session_id))
            assert caught.value.code == "session_not_found"
            accepted = await owner.request("chat.send", ChatSendParams(session_id, (TextRpcSegment("hello"),)),
                idempotency_key="one-turn")
            assert isinstance(accepted, ChatSendResult)
            for _ in range(100):
                result = await owner.request("runs.get", RunsGetParams(session_id, accepted.run_id))
                assert isinstance(result, RunsGetResult)
                if result.run.state == "completed":
                    break
                await asyncio.sleep(0.01)
            assert result.run.state == "completed"
            assert executor.requests[0].principal.role is Role.OWNER
            assert executor.requests[0].principal.user_id == "avatar-owner"
            assert host.state_store.get_run(accepted.run_id).result["final_text"]
            assert not host.state_store.find_outbound_deliveries(session_id=session_id, run_id=accepted.run_id)
        finally:
            await owner.close()
            await other.close()
            await invalid.close()
            await host.stop()
        assert not host.ready
        lease = host.state_store.acquire_instance_lease()
        lease.close()

    asyncio.run(exercise())


def test_real_application_materializes_and_reuses_an_owner_client_session(tmp_path, monkeypatch):
    from chatcopilot.protocols.gateway_client import GatewayWebSocketClient
    from integrations.open_llm_vtuber.agentstrata_vtuber.bridge import ConversationBridge
    from tests.unit.test_application_actor_runtime import _FakeAgentRuntime

    spec = load_botspec(_write_spec(tmp_path, _client_data()))
    runtime = assemble_runtime_context(spec)
    agent = _FakeAgentRuntime()
    agent.close = lambda: None
    monkeypatch.setattr(runtime_module, "load_config", lambda **_: ChatConfig())
    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", lambda *_, **__: agent)
    original = runtime_module.GatewayServerConfig
    monkeypatch.setattr(runtime_module, "GatewayServerConfig", lambda **kw: original(**{**kw, "port": 0}))
    environment = _environment(tmp_path)
    environment["AVATAR_TOKEN"] = "a" * 48

    async def exercise():
        host = build_gateway_runtime_host(runtime, environ=environment)
        await host.start()
        bridge = ConversationBridge(GatewayWebSocketClient(GatewayClientConfig(
            f"ws://127.0.0.1:{host.server.health().port}", "a" * 48,
            client_id="avatar-owner", client_mode="interactive")), client_id="avatar-owner")
        bridge.select_history("character", "history")
        try:
            assert (await bridge.chat("first")).text == "reply:first"
            assert (await bridge.chat("second")).text == "reply:second"
            assert len(agent.sessions) == 1
            assert [task.text for task in agent.sessions[0].tasks] == ["first", "second"]
            assert agent.creations[0]["caller_role_hint"] == "owner"
            assert (environment["CHATCOPILOT_WORKSPACE_ROOT"] + "/p2p_avatar-owner") == str(
                agent.creations[0]["workspace_service"].workspace.root)
        finally:
            await bridge.close()
            await host.stop()
    asyncio.run(exercise())
