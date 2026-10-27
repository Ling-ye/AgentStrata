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


def test_vtuber_service_description_exports_its_observable_agent(tmp_path):
    from chatcopilot.core.install_artifacts import write_private
    from integrations.open_llm_vtuber.agentstrata_vtuber.deployment import service_definitions

    write_private(tmp_path / "config/configured.json", json.dumps({"port": 12393, "gateway_port": 18791}))
    result = service_definitions({"root": str(tmp_path), "host": {"python": "/usr/bin/python3"}})
    assert result["observable_agents"] == [{"id": "main", "display_name": "Live2D Assistant",
        "bot_spec": "config/bot.yaml", "env_file": "config/runtime.env"}]
    assert [s["name"] for s in result["services"]] == ["client", "gateway"]


@pytest.mark.parametrize("runtime_id", ["langgraph", "codex"])
@pytest.mark.parametrize("auth_mode", ["api_key", "chatgpt"])
def test_vtuber_keeps_application_profile_and_model_snapshot_independent(tmp_path, monkeypatch, runtime_id, auth_mode):
    import sys
    import yaml
    from chatcopilot.botspec.loader import load_botspec
    from chatcopilot.core.settings import load_local_env_values
    from integrations.open_llm_vtuber.agentstrata_vtuber import assets
    from integrations.open_llm_vtuber.agentstrata_vtuber.deployment import configure

    monkeypatch.setattr(assets, "prepare_assets", lambda *args: None)
    root = tmp_path / "installation"
    backend = root / "runtime/apps/backend"
    (backend / "config_templates").mkdir(parents=True)
    (backend / "config_templates/conf.ZH.default.yaml").write_text(
        "system_config: {}\ncharacter_config:\n  agent_config: {}\n")
    native = tmp_path / "codex"
    native.write_bytes(b"\x7fELFfixture")
    monkeypatch.setenv("FIXTURE_CODEX", str(native))
    monkeypatch.setenv("FIXTURE_API_KEY", "synthetic-model-credential")
    connection = {"kind": "codex" if auth_mode == "chatgpt" else "openai_responses",
                  "auth": ({"mode": "chatgpt", "profile": "main"} if auth_mode == "chatgpt" else
                           {"mode": "api_key", "key_env": "FIXTURE_API_KEY"}),
                  "codex_bin_env": "FIXTURE_CODEX"}
    if runtime_id == "langgraph":
        monkeypatch.setattr("integrations.open_llm_vtuber.agentstrata_vtuber.deployment._native_codex",
            lambda *_: pytest.fail("LangGraph model authentication must not start or require Codex"))
    host_models = tmp_path / "host-models.json"
    document = {"connections": {"fixture": connection},
                "profiles": {"fixture": {"connection": "fixture", "model": "original-model"}},
                "bindings": {"chat": "fixture"}}
    host_models.write_text(json.dumps(document))
    request = {"root": str(root), "host": {"model_config": str(host_models), "model_binding": "chat",
               "source_url": "https://example.com/source", "python": sys.executable},
               "options": {"port": 12393, "gateway_port": 18791, "runtime": runtime_id}}
    configure(request, {"backend": backend})
    app_path = root / "config/bot.yaml"
    assert set(yaml.safe_load(app_path.read_text())).isdisjoint({"llm", "prompts", "tools", "agents", "context"})
    spec = load_botspec(app_path)
    assert spec.agents.runtime == runtime_id
    assert spec.application.agent_profile.source_path == root / "config/agent.yaml"
    environment = load_local_env_values(root / "config/runtime.env", expand_home=True)
    assert environment["AGENTSTRATA_LLM_CONFIG"] == str(root / "config/model.json")
    snapshot = (root / "config/model.json").read_bytes()
    assert b"synthetic-model-credential" not in snapshot
    document["profiles"]["fixture"]["model"] = "changed-host-model"
    host_models.write_text(json.dumps(document))
    configure(request, {"backend": backend})
    assert (root / "config/model.json").read_bytes() == snapshot
    assert json.loads(snapshot)["profiles"]["fixture"]["model"] == "original-model"


class _Gateway:
    def __init__(self, *, lose_ack=False, running=False, text="final answer"):
        self.calls = []
        self.turns = {}
        self.runs = {}
        self.lose_ack = lose_ack
        self.running = running
        self.accepted = asyncio.Event()
        self.closed = False
        self.text = text

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
                    "running" if self.running else "completed", (TextRpcSegment(self.text),))
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


@pytest.mark.parametrize("user_input,text", [
    ("/source", "AgentStrata AGPL-3.0-only\nSource: https://example.com/source"),
    ("hello", "回复。\n\n本服务源码：https://example.com/source"),
])
def test_source_response_and_first_notice_remain_complete(user_input, text):
    async def exercise():
        gateway = _Gateway(text=text)
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        assert (await bridge.chat(user_input)).text == text
        sends = [params for method, params, _ in gateway.calls if method == "chat.send"]
        assert len(sends) == 1
        assert sends[0].segments == (TextRpcSegment(user_input),)
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


def test_stop_during_preflight_prevents_chat_submission():
    class Preflight(_Gateway):
        def __init__(self):
            super().__init__()
            self.preparing, self.release = asyncio.Event(), asyncio.Event()

        async def request(self, method, params, *, idempotency_key=None):
            if method == "sessions.create":
                self.preparing.set()
                await self.release.wait()
            return await super().request(method, params, idempotency_key=idempotency_key)

    async def exercise():
        gateway = Preflight()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        task = asyncio.create_task(bridge.chat("stop before submission"))
        await gateway.preparing.wait()
        await bridge.cancel()
        gateway.release.set()
        result, = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)
        assert not gateway.turns
        assert not any(method == "chat.send" for method, _, _ in gateway.calls)
        await bridge.close()
    asyncio.run(exercise())


def test_stop_during_submission_is_applied_when_original_acceptance_arrives():
    class DelayedAcceptance(_Gateway):
        def __init__(self):
            super().__init__(running=True)
            self.release = asyncio.Event()

        async def request(self, method, params, *, idempotency_key=None):
            result = await super().request(method, params, idempotency_key=idempotency_key)
            if method == "chat.send":
                await self.release.wait()
            return result

    async def exercise():
        gateway = DelayedAcceptance()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        task = asyncio.create_task(bridge.chat("original"))
        await gateway.accepted.wait()
        with pytest.raises(RuntimeError, match="unknown"):
            await bridge.cancel()
        gateway.release.set()
        try:
            reply = await asyncio.wait_for(task, 1)
            assert reply.state == "aborted"
            assert len(gateway.turns) == 1
            assert len([1 for method, _, _ in gateway.calls if method == "chat.send"]) == 1
        finally:
            await bridge.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("result_kind", ["rejected", "wrong_run", "wrong_type", "recovery_required"])
def test_stop_requires_a_matching_confirmed_result(result_kind):
    class Unconfirmed(_Gateway):
        async def request(self, method, params, *, idempotency_key=None):
            if method == "chat.abort":
                return {"rejected": ChatAbortResult(params.session_id, params.run_id, False),
                        "wrong_run": ChatAbortResult(params.session_id, "another-run", True),
                        "wrong_type": RunsLatestResult(None)}[result_kind]
            return await super().request(method, params, idempotency_key=idempotency_key)

    async def exercise():
        gateway = Unconfirmed()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        gateway.runs[bridge.session_id] = RunSnapshot(bridge.session_id, "original",
            "recovery_required" if result_kind == "recovery_required" else "running")
        with pytest.raises(RuntimeError):
            await bridge.cancel()
        assert not gateway.turns
        await bridge.close()
    asyncio.run(exercise())


def test_stop_during_reconnect_does_not_retry_unknown_input_until_explicit_resume():
    class LostAcceptance(_Gateway):
        def __init__(self):
            super().__init__()
            self.reconnecting, self.release = asyncio.Event(), asyncio.Event()
            self.attempts = []

        async def ensure_connected(self):
            if len(self.attempts) == 1 and not self.release.is_set():
                self.reconnecting.set()
                await self.release.wait()

        async def request(self, method, params, *, idempotency_key=None):
            if method == "chat.send":
                self.attempts.append((params, idempotency_key))
                if len(self.attempts) == 1:
                    raise GatewayMutationOutcomeUnknown("mutation_outcome_unknown", "lost acceptance")
            return await super().request(method, params, idempotency_key=idempotency_key)

    async def exercise():
        gateway = LostAcceptance()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        task = asyncio.create_task(bridge.chat("original"))
        await gateway.reconnecting.wait()
        with pytest.raises(RuntimeError, match="unknown"):
            await bridge.cancel()
        gateway.release.set()
        with pytest.raises(RuntimeError, match="unknown"):
            await task
        assert len(gateway.attempts) == 1 and not gateway.turns
        assert (await bridge.resume()).state == "completed"
        assert gateway.attempts[0] == gateway.attempts[1]
        assert len(gateway.turns) == 1
        await bridge.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("closing", [False, True])
def test_late_stop_receipt_cannot_clear_a_new_request_and_close_blocks_new_work(closing):
    class DelayedStop(_Gateway):
        def __init__(self):
            super().__init__(running=True)
            self.stopping, self.release = asyncio.Event(), asyncio.Event()
            self.finished = None

        async def request(self, method, params, *, idempotency_key=None):
            if method == "chat.abort" and params.run_id == "run-0":
                self.stopping.set()
                await self.release.wait()
                return ChatAbortResult(params.session_id, params.run_id, False)
            if method == "runs.get" and params.run_id == "run-0" and self.finished is not None:
                return RunsGetResult(self.finished)
            return await super().request(method, params, idempotency_key=idempotency_key)

    async def exercise():
        gateway = DelayedStop()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        first = asyncio.create_task(bridge.chat("first"))
        await gateway.accepted.wait()
        stopping = asyncio.create_task(bridge.close() if closing else bridge.cancel())
        await gateway.stopping.wait()
        gateway.finished = replace(gateway.runs[bridge.session_id], state="completed")
        assert (await asyncio.wait_for(first, 1)).state == "completed"
        gateway.runs[bridge.session_id] = gateway.finished
        gateway.accepted.clear()
        second = asyncio.create_task(bridge.chat("second"))
        try:
            if closing:
                with pytest.raises(RuntimeError, match="closed"):
                    await asyncio.wait_for(second, 1)
                assert len(gateway.turns) == 1
            else:
                await asyncio.wait_for(gateway.accepted.wait(), 1)
                pending = bridge._turn
            gateway.release.set()
            await stopping
            if not closing:
                assert bridge._turn is pending
                await bridge.cancel()
                assert (await asyncio.wait_for(second, 1)).state == "aborted"
                assert len(gateway.turns) == 2
        finally:
            gateway.release.set()
            await stopping
            if not second.done():
                second.cancel()
                await asyncio.gather(second, return_exceptions=True)
            await bridge.close()
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


def test_cancel_during_resume_lookup_is_not_blocked_by_the_observation_lock():
    class DelayedLookup(_Gateway):
        def __init__(self):
            super().__init__(running=True)
            self.looking, self.release = asyncio.Event(), asyncio.Event()

        async def request(self, method, params, *, idempotency_key=None):
            if method == "runs.latest":
                self.looking.set()
                await self.release.wait()
            return await super().request(method, params, idempotency_key=idempotency_key)

    async def exercise():
        gateway = DelayedLookup()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        gateway.runs[bridge.session_id] = RunSnapshot(bridge.session_id, "original", "running")
        resuming = asyncio.create_task(bridge.resume())
        await gateway.looking.wait()
        try:
            with pytest.raises(RuntimeError, match="unknown"):
                await asyncio.wait_for(bridge.cancel(), 1)
        finally:
            gateway.release.set()
        assert (await asyncio.wait_for(resuming, 1)).state == "aborted"
        assert not any(method == "chat.send" for method, _, _ in gateway.calls)
        await bridge.close()
    asyncio.run(exercise())


def test_cancelled_waiter_allows_next_input_only_after_terminal_confirmation():
    async def exercise():
        gateway = _Gateway(running=True)
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        task = asyncio.create_task(bridge.chat("first"))
        await gateway.accepted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gateway.running = False
        reply = await bridge.chat("second")
        assert reply.state == "completed" and len(gateway.turns) == 2
        first_abort = next(i for i, (method, _, _) in enumerate(gateway.calls) if method == "chat.abort")
        assert gateway.calls[first_abort + 1][0] == "runs.get"
        await bridge.close()
    asyncio.run(exercise())


def test_stop_while_rechecking_previous_terminal_prevents_the_next_input():
    class DelayedTerminal(_Gateway):
        def __init__(self):
            super().__init__(running=True)
            self.checking, self.release = asyncio.Event(), asyncio.Event()
            self.delay_next = False

        async def request(self, method, params, *, idempotency_key=None):
            if method == "runs.get" and self.delay_next:
                self.delay_next = False
                self.checking.set()
                await self.release.wait()
            return await super().request(method, params, idempotency_key=idempotency_key)

    async def exercise():
        gateway = DelayedTerminal()
        bridge = ConversationBridge(gateway, client_id="avatar")
        bridge.select_history("character", "first")
        first = asyncio.create_task(bridge.chat("first"))
        await gateway.accepted.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        gateway.delay_next = True
        second = asyncio.create_task(bridge.chat("must not be submitted"))
        await gateway.checking.wait()
        await bridge.cancel()
        gateway.release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(second, 1)
            assert len(gateway.turns) == 1
        finally:
            await bridge.close()
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
        assert (await check("http", "/", [], ".".join(("10", "0", "0", "1"))))[0]["status"] == 403
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
                        assert {tool.name for tool in tools.tools} == {"list_actions", "set_expression", "play_motion", "apply_pose"}
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


@pytest.mark.parametrize('expression_status,motion_status,expected_calls', [
    ('executed', 'executed', ['expression', 'motion']),
    ('unknown', 'executed', ['expression']),
    ('failed', 'executed', ['expression']),
    ('executed', 'unknown', ['expression', 'motion']),
    ('executed', 'failed', ['expression', 'motion']),
])
def test_pose_keeps_partial_receipts_and_never_replays_unknown_actions(expression_status, motion_status, expected_calls):
    from fastapi import FastAPI
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def exercise():
        calls = []
        class Avatar:
            def list_actions(self):
                return {'connected': True, 'expressions': ['happy'], 'motions': ['greet']}
            async def set_expression(self, name):
                calls.append('expression')
                return {'request_id': 'expression-receipt', 'status': expression_status}
            async def play_motion(self, name):
                calls.append('motion')
                return {'request_id': 'motion-receipt', 'status': motion_status}
        app = FastAPI()
        install_control(app, _config(), Avatar())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)),
                    headers={'Authorization': 'Bearer ' + 'm' * 48}) as http:
                async with streamable_http_client('http://127.0.0.1:12393/agentstrata/mcp/', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool('apply_pose', {'expression': 'happy', 'motion': 'greet'})
                        body = json.loads(result.content[0].text)
                        assert calls == expected_calls
                        assert all(name + '-receipt' in json.dumps(body) for name in expected_calls)
                        if expected_calls == ['expression']:
                            assert 'motion-receipt' not in json.dumps(body)
                        assert body['status'] == (motion_status if expression_status == 'executed' else expression_status)
                        assert result.isError is (expression_status != 'executed' or motion_status != 'executed')
    asyncio.run(exercise())
