"""Optional adapter contracts run without installing the upstream voice application."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.gateway_rpc import (
    ChatAbortResult, ChatSendResult, RunSnapshot, RunsGetResult, RunsLatestResult,
    SessionSnapshot, SessionsCreateResult, TextRpcSegment,
)
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayMutationOutcomeUnknown
from integrations.open_llm_vtuber.agentstrata_vtuber.avatar import BrowserAvatarController
from integrations.open_llm_vtuber.agentstrata_vtuber.bridge import ConversationBridge
from integrations.open_llm_vtuber.agentstrata_vtuber.config import AdapterConfig, Motion
from integrations.open_llm_vtuber.agentstrata_vtuber.control import LocalBoundary, install_control


def _config():
    return AdapterConfig(GatewayClientConfig("ws://127.0.0.1:18791", "g" * 48,
        client_id="avatar", client_mode="interactive"), "m" * 48,
        "mao_pro", {"happy": 0}, {"greet": Motion("TapBody", 0)},
        ("http://localhost:12393",))


class _Gateway:
    def __init__(self, *, lose_ack=False, running=False):
        self.calls = []
        self.turns = {}
        self.runs = {}
        self.lose_ack = lose_ack
        self.running = running
        self.accepted = asyncio.Event()
        self.closed = False

    async def ensure_connected(self):
        pass

    async def request(self, method, params, *, idempotency_key=None):
        self.calls.append((method, params, idempotency_key))
        if method == "sessions.create":
            return SessionsCreateResult(SessionSnapshot(params.session_id,
                ChannelAccountRef("gateway-client", "avatar"), ConversationRef("p2p", params.session_id),
                "default", False, 0))
        if method == "runs.latest":
            return RunsLatestResult(self.runs.get(params.session_id))
        if method == "chat.send":
            if idempotency_key not in self.turns:
                run = RunSnapshot(params.session_id, "run-" + str(len(self.turns)),
                    "running" if self.running else "completed", (TextRpcSegment("final answer"),))
                self.turns[idempotency_key] = run
                self.runs[params.session_id] = run
            run = self.turns[idempotency_key]
            self.accepted.set()
            if self.lose_ack:
                self.lose_ack = False
                raise GatewayMutationOutcomeUnknown("mutation_outcome_unknown", "lost acknowledgement")
            return ChatSendResult(params.session_id, run.run_id)
        if method == "runs.get":
            return RunsGetResult(self.runs[params.session_id])
        if method == "chat.abort":
            self.runs[params.session_id] = replace(self.runs[params.session_id], state="aborted", segments=())
            return ChatAbortResult(params.session_id, params.run_id, True)
        raise AssertionError(method)

    async def close(self):
        self.closed = True


def test_history_mapping_and_final_reply_do_not_import_upstream():
    async def exercise():
        gateway = _Gateway()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        first = bridge.session_id
        reply = await bridge.chat("hello")
        assert reply.text == "final answer"
        bridge.select_history("character", "second")
        assert first != bridge.session_id
        bridge.select_history("character", "first")
        assert first == bridge.session_id
        await bridge.close()
        assert gateway.closed
    asyncio.run(exercise())


def test_lost_acceptance_reuses_exact_request_and_never_creates_another_turn():
    async def exercise():
        gateway = _Gateway(lose_ack=True)
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        reply = await bridge.chat("one request")
        sends = [(params, key) for method, params, key in gateway.calls if method == "chat.send"]
        assert len(sends) == 2 and sends[0] == sends[1]
        assert len(gateway.turns) == 1 and reply.state == "completed"
    asyncio.run(exercise())


def test_unfinished_history_is_observed_instead_of_replaced_and_can_be_stopped():
    async def exercise():
        gateway = _Gateway(running=True)
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        task = asyncio.create_task(bridge.chat("original"))
        await gateway.accepted.wait()
        with pytest.raises(RuntimeError, match="Stop"):
            bridge.select_history("character", "second")
        reconnected = ConversationBridge(gateway, client_id="avatar")
        reconnected.select_history("character", "first")
        with pytest.raises(RuntimeError, match="unfinished"):
            await reconnected.chat("replacement must not execute")
        await reconnected.cancel()
        assert (await task).state == "aborted"
        assert len(gateway.turns) == 1
    asyncio.run(exercise())


def test_empty_transcription_has_no_gateway_effects():
    async def exercise():
        gateway = _Gateway()
        bridge = ConversationBridge(gateway, client_id="avatar")
        with pytest.raises(ValueError, match="Empty"):
            await bridge.chat("  ")
        assert gateway.calls == []
    asyncio.run(exercise())


def test_cancelling_during_unknown_acceptance_does_not_submit_work_to_cancel_it():
    class BlockedSend(_Gateway):
        def __init__(self):
            super().__init__()
            self.submission_started = asyncio.Event()
            self.sends = 0
        async def request(self, method, params, *, idempotency_key=None):
            if method == "chat.send":
                self.sends += 1
                self.submission_started.set()
                await asyncio.Future()
            return await super().request(method, params, idempotency_key=idempotency_key)
    async def exercise():
        gateway = BlockedSend()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        task = asyncio.create_task(bridge.chat("must not start after cancellation"))
        await gateway.submission_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await bridge.close()
        assert gateway.sends == 1 and not gateway.turns and gateway.closed
    asyncio.run(exercise())


def test_avatar_capabilities_do_not_require_an_attached_browser():
    async def exercise():
        controller = BrowserAvatarController(_config())
        assert controller.list_actions()["expressions"] == ["happy"]
        assert (await controller.play_motion("greet"))["status"] == "unavailable"
        assert (await controller.play_motion("../../file"))["error"] == "unknown_motion"
        with pytest.raises(ValueError, match="model"):
            controller.attach("browser", "other-model", lambda _: None)
    asyncio.run(exercise())


def test_motion_waits_for_its_own_browser_callback_without_blocking_other_actions():
    async def exercise():
        controller = BrowserAvatarController(_config())
        messages = asyncio.Queue()

        async def send(data):
            await messages.put(json.loads(data))

        controller.attach("browser", "mao_pro", send)
        motion = asyncio.create_task(controller.play_motion("greet"))
        expression = asyncio.create_task(controller.set_expression("happy"))
        first, second = await messages.get(), await messages.get()
        assert not motion.done() and not expression.done()
        assert not controller.acknowledge("different-browser", {**first, "status": "executed"})
        assert controller.acknowledge("browser", {**first, "status": "executed"})
        assert not controller.acknowledge("browser", {**first, "status": "executed"})
        assert controller.acknowledge("browser", {**second, "status": "failed"})
        assert {result["status"] for result in await asyncio.gather(motion, expression)} == {"executed", "failed"}
        assert not controller._pending
    asyncio.run(exercise())


def test_disconnect_and_timeout_report_unknown_without_claiming_execution():
    async def exercise():
        controller = BrowserAvatarController(replace(_config(), action_timeout_seconds=0.01))
        sent = asyncio.Event()

        async def send(data):
            sent.set()

        controller.attach("browser", "mao_pro", send)
        task = asyncio.create_task(controller.play_motion("greet"))
        await sent.wait()
        controller.detach("browser")
        assert (await task)["status"] == "unknown"
        controller.attach("new-browser", "mao_pro", send)
        assert (await controller.set_expression("happy"))["status"] == "unknown"
        assert not controller._pending
        pending = asyncio.create_task(controller.play_motion("greet"))
        await asyncio.sleep(0)
        request_id = next(iter(controller._pending))
        assert controller.acknowledge("new-browser", {"request_id": request_id, "status": "unknown"})
        result = await pending
        assert result["status"] == "unknown" and "error" not in result
    asyncio.run(exercise())


def test_browser_origin_and_control_credential_are_checked_before_side_effects():
    async def exercise():
        reached = []
        async def application(scope, receive, send):
            reached.append(scope["path"])
        boundary = LocalBoundary(application, config=_config())
        async def check(kind, path, headers, peer="127.0.0.1"):
            messages = []
            async def send(message):
                messages.append(message)
            await boundary({"type": kind, "path": path, "headers": headers, "client": (peer, 1234)}, None, send)
            return messages
        assert (await check("websocket", "/client-ws", [(b"origin", b"https://attacker.invalid")]))[0]["code"] == 1008
        assert (await check("http", "/agentstrata/mcp/", []))[0]["status"] == 403
        assert (await check("http", "/", [], "10.0.0.1"))[0]["status"] == 403
        assert reached == []
        await check("websocket", "/client-ws", [(b"origin", b"http://localhost:12393")])
        await check("http", "/agentstrata/mcp/", [(b"authorization", ("Bearer " + "m" * 48).encode())])
        assert reached == ["/client-ws", "/agentstrata/mcp/"]
    asyncio.run(exercise())


@pytest.mark.parametrize("replacement", [False, True])
def test_real_mcp_tools_can_start_without_gateway_or_browser(replacement):
    from fastapi import FastAPI
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def exercise():
        app = FastAPI()
        class ReplacementAvatar:
            def list_actions(self):
                return {"connected": True, "expressions": ["happy"], "motions": ["greet"]}
            async def set_expression(self, name):
                return {"status": "executed"}
            async def play_motion(self, name):
                return {"status": "executed"}
        controller = ReplacementAvatar() if replacement else BrowserAvatarController(_config())
        install_control(app, _config(), controller)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                    headers={"Authorization": "Bearer " + "m" * 48}) as http:
                async with streamable_http_client("http://127.0.0.1:12393/agentstrata/mcp/", http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {tool.name for tool in tools.tools} == {"list_actions", "set_expression", "play_motion"}
                        listed = await session.call_tool("list_actions", {})
                        assert json.loads(listed.content[0].text)["connected"] is replacement
                        action = await session.call_tool("play_motion", {"name": "greet"})
                        assert action.isError is not replacement
                        assert json.loads(action.content[0].text)["status"] == ("executed" if replacement else "unavailable")
    asyncio.run(exercise())


def test_config_loads_env_references_and_rejects_shared_credentials(tmp_path):
    data = {"gateway_url": "ws://127.0.0.1:18791", "client_id": "avatar",
        "gateway_token_env": "GATEWAY_TOKEN", "mcp_token_env": "AVATAR_TOKEN", "model_name": "mao_pro",
        "allowed_origins": ["http://localhost:12393"], "expressions": {"happy": 0},
        "motions": {"greet": {"group": "TapBody", "index": 0}}}
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(data))
    config = AdapterConfig.load(path, {"GATEWAY_TOKEN": "g" * 48, "AVATAR_TOKEN": "m" * 48})
    assert config.motions["greet"] == Motion("TapBody", 0)
    assert "m" * 20 not in repr(config) and "g" * 20 not in repr(config)
    with pytest.raises(ValueError, match="differ"):
        AdapterConfig.load(path, {"GATEWAY_TOKEN": "g" * 48, "AVATAR_TOKEN": "g" * 48})


def test_disabling_avatar_control_keeps_client_app_and_dialogue_independent():
    from fastapi import FastAPI
    async def exercise():
        config = replace(_config(), mcp_token=None)
        app = FastAPI()
        install_control(app, config, BrowserAvatarController(config))
        assert not any(getattr(route, "path", "").startswith("/agentstrata/mcp") for route in app.routes)
        gateway = _Gateway()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "plain-text")
        assert (await bridge.chat("no avatar tool required")).text == "final answer"
    asyncio.run(exercise())
