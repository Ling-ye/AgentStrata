"""Production bridge/host/runtime assembly with controlled model, app-server and OneBot peers.

No production credentials, real model, QQ account or audio device are used.
The selected RuntimeAdapter, Application, Gateway and WebSocket edges are real.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import socket
import subprocess
from threading import Event

import pytest
import websockets

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.runtime import assemble_runtime_context
from chatcopilot.contracts.gateway_rpc import RunsGetParams
from chatcopilot.core.llm_client import ChatResult, LLMClient
from chatcopilot.host.runtime import build_gateway_runtime_host
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayClientError, GatewayWebSocketClient
from integrations.open_llm_vtuber.agentstrata_vtuber.bridge import ConversationBridge
from tests.application_fixture import write_application
from tests.codex_app_server_fixture import app_server_replay
from tests.integration.test_gateway_onebot_roundtrip import _FakeOneBotProvider, _BOT, _ACTOR


@asynccontextmanager
async def _host(tmp_path, monkeypatch, *, application, runtime_id, blocking=False):
    root = tmp_path / application
    root.mkdir(mode=0o700)
    (root / "workspace").mkdir(mode=0o700)
    (root / "identity.md").write_text(application)
    (root / "style.md").write_text("Answer clearly.")
    models = root / "models.json"
    models.write_text(json.dumps({
        "connections": {"bridge": {"kind": "openai_responses",
            "auth": {"mode": "api_key", "key_env": "BRIDGE_MODEL_KEY"}}},
        "profiles": {"bridge": {"connection": "bridge", "model": "bridge-model"}},
        "bindings": {"bridge.chat": "bridge"},
    }))
    token = "g" * 48
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    declaration = {
        "id": "bridge-app", "display_name": "Bridge baseline",
        "gateway": {"clients": [{"id": "avatar", "token_env": "BRIDGE_GATEWAY_TOKEN", "role": "owner"},
                                 {"id": "other", "token_env": "BRIDGE_OTHER_TOKEN", "role": "user"}]},
        "workspace": {"root_env": "CHATCOPILOT_WORKSPACE_ROOT"},
        "llm": {"chat": {"binding": "bridge.chat"}, "research": {"binding": "bridge.chat"}},
        "agents": {"runtime": runtime_id}, "tools": {"packs": []},
        "prompts": {"schema_version": 2, "identity": "identity.md", "response_style": "style.md"},
    }
    environment = {
        "AGENTSTRATA_LLM_CONFIG": str(models), "BRIDGE_MODEL_KEY": "synthetic-bridge-credential",
        "BRIDGE_GATEWAY_TOKEN": token, "BRIDGE_OTHER_TOKEN": "h" * 48,
        "CHATCOPILOT_GATEWAY_TOKEN": "a" * 48, "CHATCOPILOT_GATEWAY_PORT": str(port),
        "CHATCOPILOT_GATEWAY_STATE_ROOT": str(root / "state"),
        "CHATCOPILOT_WORKSPACE_ROOT": str(root / "workspace"),
    }
    peer = server = None
    if application == "qq":
        peer = _FakeOneBotProvider()
        server = await websockets.serve(peer.handler, "127.0.0.1", 0)
        environment.update({"QQ_ACCOUNT": _BOT, "QQ_ACCESS_TOKEN": "o" * 32, "QQ_ALLOW_FROM": "",
            "CHATCOPILOT_QQ_ONEBOT_WS_URL": f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"})
        declaration["channels"] = {"qq": {"type": "qq_personal", "provider": "onebot_v11",
            "endpoint_env": "CHATCOPILOT_QQ_ONEBOT_WS_URL", "access_token_env": "QQ_ACCESS_TOKEN",
            "account_env": "QQ_ACCOUNT", "mention_only_groups": True}}
    path = root / "bot.yaml"
    write_application(path, declaration)
    started, release = Event(), Event()
    calls = []
    if not blocking:
        release.set()

    def wait_for_release(check_cancel):
        started.set()
        for _ in range(500):
            check_cancel()
            if release.wait(0.01):
                return
        raise AssertionError("Controlled model was not released")

    def model_chat(_client, **kwargs):
        calls.append("langgraph")
        assert kwargs["messages"]
        token = kwargs.get("cancellation")
        wait_for_release(token.raise_if_cancelled if token is not None else lambda: None)
        return ChatResult(content="bridge baseline reply")

    def codex_turn(command, **kwargs):
        calls.append("codex")
        wait_for_release(kwargs["on_poll"])
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "item.completed",
            "item": {"type": "agent_message", "text": "bridge baseline reply"}}), "")

    monkeypatch.setenv("CHATCOPILOT_ADD_OWNER_IDS", _ACTOR)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_URL", "https://example.com/source")
    monkeypatch.setattr(LLMClient, "chat", model_chat)
    monkeypatch.setattr("chatcopilot.external_tools.codex_cli.command._resolve_executable", lambda *_: "/usr/bin/true")
    monkeypatch.setattr("chatcopilot.agent.runtimes.codex.run_app_server", app_server_replay(codex_turn))
    host = None
    client = GatewayWebSocketClient(GatewayClientConfig(f"ws://127.0.0.1:{port}", token,
        client_id="avatar", client_mode="interactive"))
    bridge = ConversationBridge(client, client_id="avatar")
    try:
        host = build_gateway_runtime_host(assemble_runtime_context(load_botspec(path)), environ=environment)
        assert host.agent_runtime.runtime_id == runtime_id
        assert host.agent_runtime.runtime_config.llm.model == "bridge-model"
        await host.start()
        assert host.ready
        yield host, bridge, peer, calls, started, release
    finally:
        release.set()
        await bridge.close()
        if host is not None:
            await host.stop()
        if server is not None:
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize("runtime_id", ["langgraph", "codex"])
@pytest.mark.parametrize("application", ["live2d", "qq"])
def test_each_bridge_uses_selected_runtime_and_keeps_delivery_evidence(tmp_path, monkeypatch, runtime_id, application):
    async def exercise():
        async with _host(tmp_path, monkeypatch, application=application, runtime_id=runtime_id) as (host, bridge, peer, calls, _, _):
            if application == "live2d":
                bridge.select_history("avatar", "history")
                reply = await asyncio.wait_for(bridge.chat("hello from avatar"), 10)
                assert reply.state == "completed" and "bridge baseline reply" in reply.text
                bridge.select_history("avatar", "second-history")
                assert bridge.session_id != reply.session_id
                sessions = host.state_store.list_sessions()
                assert sessions[0].account.channel == "gateway"
            else:
                peer.emit_message.set()
                await asyncio.wait_for(peer.outbound_received.wait(), 10)
                for _ in range(200):
                    receipts = [e.payload["stage"] for e in host.state_store.events_after(0, limit=100)
                                if e.event == "delivery.updated"]
                    if receipts and receipts[-1] == "provider_acknowledged":
                        break
                    await asyncio.sleep(0.01)
                assert receipts == ["gateway_accepted", "provider_submitted", "provider_acknowledged"]
                assert "bridge baseline reply" in json.dumps(peer.actions[-1]["params"])
                assert not peer.errors
            assert calls == [runtime_id]
    asyncio.run(exercise())


@pytest.mark.parametrize("runtime_id", ["langgraph", "codex"])
def test_live2d_credentials_and_history_scope_are_checked_before_model_execution(tmp_path, monkeypatch, runtime_id):
    async def exercise():
        async with _host(tmp_path, monkeypatch, application="live2d", runtime_id=runtime_id) as (_, bridge, _, calls, _, _):
            bridge.select_history("avatar", "history")
            reply = await bridge.chat("original")
            for token, claimed_id in (("x" * 48, "avatar"), ("h" * 48, "avatar")):
                wrong = GatewayWebSocketClient(GatewayClientConfig(bridge.client.config.url, token,
                    client_id=claimed_id, client_mode="interactive"))
                try:
                    with pytest.raises(GatewayClientError):
                        await wrong.ensure_connected()
                finally:
                    await wrong.close()
            other = GatewayWebSocketClient(GatewayClientConfig(bridge.client.config.url, "h" * 48,
                client_id="other", client_mode="interactive"))
            try:
                await other.ensure_connected()
                with pytest.raises(GatewayClientError):
                    await other.request("runs.get", RunsGetParams(reply.session_id, reply.run_id))
            finally:
                await other.close()
            assert calls == [runtime_id]
    asyncio.run(exercise())


@pytest.mark.parametrize("runtime_id", ["langgraph", "codex"])
def test_qq_host_stop_cancels_selected_runtime_without_claiming_delivery(tmp_path, monkeypatch, runtime_id):
    async def exercise():
        async with _host(tmp_path, monkeypatch, application="qq", runtime_id=runtime_id, blocking=True) as (host, _, peer, calls, started, _):
            peer.emit_message.set()
            assert await asyncio.to_thread(started.wait, 5)
            await asyncio.wait_for(host.stop(), 5)
            runs = [host.state_store.latest_run_for_session(s.session_id) for s in host.state_store.list_sessions()]
            assert len(runs) == 1 and runs[0].state == "aborted"
            assert calls == [runtime_id]
            assert not any(item["action"] == "send_msg" for item in peer.actions)
    asyncio.run(exercise())


@pytest.mark.parametrize("runtime_id", ["langgraph", "codex"])
def test_live2d_stop_cancels_selected_runtime_without_starting_another_turn(tmp_path, monkeypatch, runtime_id):
    async def exercise():
        async with _host(tmp_path, monkeypatch, application="live2d", runtime_id=runtime_id, blocking=True) as (host, bridge, _, calls, started, _):
            bridge.select_history("avatar", "history")
            task = asyncio.create_task(bridge.chat("wait until stopped"))
            assert await asyncio.to_thread(started.wait, 5)
            await bridge.cancel()
            reply = await asyncio.wait_for(task, 5)
            assert reply.state == "aborted" and calls == [runtime_id]
            assert host.state_store.get_run(reply.run_id).state == "aborted"
    asyncio.run(exercise())
