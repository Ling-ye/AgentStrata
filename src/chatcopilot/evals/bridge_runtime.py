"""Isolated bridge Trials using the production host and configured Agent runtime."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from unittest.mock import patch

from chatcopilot.botspec.model import ChannelsSpec, CodebaseSpec, GatewayClientSpec, GatewaySpec, QQChannelSpec
from chatcopilot.contracts.gateway_rpc import RunsGetParams
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.evals.bridge_cases import component_identity, live2d_module, unavailable, validate
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.execution_support import usage_summary
from chatcopilot.evals.frozen_agent_runtime import tool_observations
from chatcopilot.evals.gateway_replay import read_run_observations
from chatcopilot.evals.image_delivery_fixture import OneBotFixtureConnection
from chatcopilot.evals.isolated_executor import _isolated_subagents
from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.trial_capture import execution_phase, record_turn
from chatcopilot.host.runtime import GatewayRuntimeConfigurationError, build_gateway_runtime_host
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayRemoteError, GatewayWebSocketClient


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _avatar_peer(port, token, *, timeout):
    """Only the browser is simulated; adapter controller and HTTP MCP remain real."""
    import uvicorn
    from starlette.applications import Starlette
    config_module = live2d_module("config")
    config = config_module.AdapterConfig(
        GatewayClientConfig("ws://127.0.0.1:1", "g" * 48, client_id="bridge-eval", client_mode="interactive"),
        token, "fixture-avatar", {"happy": 0}, {"greet": config_module.Motion("TapBody", 0)},
        (f"http://127.0.0.1:{port}",), action_timeout_seconds=1.0,
    )
    controller = live2d_module("avatar").BrowserAvatarController(config)
    actions, acknowledgements = [], []

    async def consume(message):
        action = json.loads(message)
        actions.append(action)
        ack = {"request_id": action["request_id"], "status": "executed"}
        acknowledgements.append({**ack, "matched": controller.acknowledge("eval-browser", ack)})

    controller.attach("eval-browser", "fixture-avatar", consume)
    app = Starlette()
    live2d_module("control").install_control(app, config, controller)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                         timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise unavailable("Avatar fixture did not become ready")
            time.sleep(0.01)
        yield actions, acknowledgements
    finally:
        server.should_exit = True
        thread.join(5)
        if thread.is_alive():
            raise unavailable("Avatar fixture did not stop", code="cleanup_error")


def run(definition, *, bot: str, workspace_root: Path) -> TrialObservation:
    validate(definition)
    identity = component_identity()
    runtime = load_evaluation_runtime(bot)
    root = private_directory(workspace_root / "bridge")
    workspaces = private_directory(root / "workspaces")
    wiki = private_directory(root / "wiki")
    dev = private_directory(root / "project")
    gateway_port, avatar_port = _port(), _port()
    owner_token, other_token, control_token = (secrets.token_urlsafe(48) for _ in range(3))
    application = definition.scenario_params["application"]
    gateway = GatewaySpec(clients=(GatewayClientSpec("bridge-eval", "BRIDGE_EVAL_TOKEN", "owner"),
                                   GatewayClientSpec("outsider", "BRIDGE_OTHER_TOKEN", "user")))
    qq = QQChannelSpec(action_timeout_seconds=1.0) if application == "qq" else None
    channels = ChannelsSpec(qq=qq)
    context = replace(runtime.spec.context, codebases=CodebaseSpec())
    runtime = replace(runtime, gateway=gateway, channels=channels, tool_packs=(), tool_features=(),
        mcp_servers=(), rag_sources=(), skills=(), exclude_tools=(), subagents=_isolated_subagents(runtime.subagents),
        spec=replace(runtime.spec, gateway=gateway, channels=channels, context=context))
    env = {
        gateway.port_env: str(gateway_port), gateway.state_root_env: str(root / "state"),
        gateway.token_env: "g" * 48, "BRIDGE_EVAL_TOKEN": owner_token, "BRIDGE_OTHER_TOKEN": other_token,
        runtime.spec.workspace.root_env: str(workspaces), runtime.spec.context.wiki.root_env: str(wiki),
        runtime.spec.context.dev.root_env: str(dev), "QQ_ALLOW_FROM": "20002",
        "CHATCOPILOT_ADD_OWNER_IDS": "20002", "CHATCOPILOT_ADD_ADMIN_IDS": "",
        "CHATCOPILOT_ADD_OWNER_NAMES": "", "CHATCOPILOT_ADD_ADMIN_NAMES": "",
    }
    if qq is not None:
        env.update({qq.endpoint_env: "ws://127.0.0.1:1", qq.account_env: "10001", qq.access_token_env: "q" * 48})
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        avatar = ([], [])
        if definition.scenario_id in {"avatar-action", "avatar-pose"}:
            avatar = stack.enter_context(_avatar_peer(avatar_port, control_token, timeout=definition.policy.timeout_seconds))
            runtime = replace(runtime, mcp_servers=(McpServerConfig(
                id="bridge-avatar", transport="streamable_http", url=f"http://127.0.0.1:{avatar_port}/agentstrata/mcp/",
                headers={"Authorization": "Bearer " + control_token}, exposure="main", risk="interactive", stateless_http=True,
                tool_prefix="avatar", allowed_tools=("list_actions", "play_motion", "set_expression",
                    *(("apply_pose",) if definition.scenario_id == "avatar-pose" else ())),
            ),))
        with execution_phase("runtime"):
            result = asyncio.run(_execute(definition, runtime, root, env, owner_token, other_token, avatar))
    if component_identity() != identity:
        raise unavailable("Bridge implementation changed during the Trial", code="artifact_integrity_error")
    return result


async def _execute(definition, runtime, root, env, owner_token, other_token, avatar):
    application = definition.scenario_params["application"]
    scenario = definition.scenario_id
    peer = OneBotFixtureConnection("unknown" if scenario == "delivery-unknown" else "acknowledged")

    async def connect(_config):
        return peer

    try:
        host = build_gateway_runtime_host(runtime, environ={**os.environ, **env}, onebot_connection_factory=connect)
    except (OSError, GatewayRuntimeConfigurationError) as exc:
        raise unavailable("Isolated Gateway configuration is unavailable: " + type(exc).__name__) from exc
    gateway_url = f"ws://127.0.0.1:{env[runtime.gateway.port_env]}"
    bridge = None
    state = {"application": application, "scenario": scenario, "production_delivery": False,
             "platform_peer": "controlled", "model_execution": "configured", "runs": [], "receipts": []}
    captured = []
    deadline = time.monotonic() + definition.policy.timeout_seconds

    async def until(probe):
        while time.monotonic() < deadline:
            value = probe()
            if value:
                return value
            await asyncio.sleep(0.02)
        raise unavailable("Bridge observation did not reach a verifiable outcome", code="evidence_missing")

    try:
        try:
            await host.start()
        except Exception as exc:
            raise unavailable("Isolated Gateway could not start: " + type(exc).__name__) from exc
        if application == "live2d":
            client = GatewayWebSocketClient(GatewayClientConfig(gateway_url, owner_token,
                client_id="bridge-eval", client_mode="interactive"))
            bridge = live2d_module("bridge").ConversationBridge(client, client_id="bridge-eval")
            bridge.select_history("fixture-avatar", definition.case_id)
        for index, turn in enumerate(definition.turns):
            record = {"conversation_id": definition.case_id, "turn_index": index, "input": turn.text, "completed": False}
            record_turn(record)
            client_error = None
            client_reply = None
            if bridge is not None:
                try:
                    reply = await bridge.chat(turn.text)
                    client_reply = {key: getattr(reply, key) for key in ("session_id", "run_id", "state", "text")}
                except RuntimeError as exc:
                    client_error = type(exc).__name__
            else:
                await peer.queue.put(json.dumps({"post_type": "message", "message_type": "private" if scenario == "admission" else "group",
                    "self_id": "10001", "message_id": str(index + 1), "group_id": "30003",
                    "user_id": "29999" if scenario == "admission" else "20002",
                    "message": [{"type": "at", "data": {"qq": "10001"}},
                                {"type": "text", "data": {"text": turn.text}}]}))
            if scenario == "admission":
                decisions = await until(host.state_store.list_authorization_decisions)
                state["admission_denied"] = all(not row.decision.allowed for row in decisions)
                state["session_count"] = len(host.state_store.list_sessions())
                record_turn({**record, "completed": True, "final_text": "", "stop_reason": "denied"})
                break

            def terminal():
                sessions = host.state_store.list_sessions()
                if not sessions:
                    return None
                current = host.state_store.latest_run_for_session(sessions[0].session_id)
                seen = {row["run_id"] for row in state["runs"]}
                return current if current and current.run_id not in seen and current.state in {"completed", "failed", "aborted"} else None

            current = await until(terminal)
            result = current.result or {}
            final, stop = str(result.get("final_text", "")), str(result.get("stop_reason", ""))
            events, _stages = read_run_observations(host.state_store, current.run_id)
            captured.extend(events)
            state["runs"].append({"run_id": current.run_id, "session_id": current.session_id,
                "state": current.state, "error_code": current.error_code, "final_text": final,
                "stop_reason": stop, "client_error": client_error, "client_reply": client_reply,
                "display_text": final + str(result.get("display_suffix", ""))})
            outbox = host.state_store.find_outbound_deliveries(session_id=current.session_id, run_id=current.run_id)
            state["receipts"].extend({**asdict(receipt), "run_id": current.run_id}
                for outbound in outbox for receipt in host.state_store.delivery_receipts(outbound.outbound_id))
            record_turn({**record, "completed": True, "final_text": final, "stop_reason": stop})
            if stop in {"llm_error", "runtime_error"}:
                raise unavailable("Configured model execution failed; see captured model diagnostics", code="model_execution_error")

        if scenario == "session-isolation":
            other = GatewayWebSocketClient(GatewayClientConfig(gateway_url, other_token,
                client_id="outsider", client_mode="interactive"))
            state["scope_denied"] = False
            try:
                await other.ensure_connected()
                last = state["runs"][-1]
                try:
                    await other.request("runs.get", RunsGetParams(last["session_id"], last["run_id"]))
                except GatewayRemoteError as exc:
                    state["scope_denied"] = exc.code == "session_not_found"
            finally:
                await other.close()
        state["provider_actions"] = [a for a in peer.actions if a["action"] != "get_login_info"]
        state["avatar_actions"], state["avatar_acknowledgements"] = avatar
        last = state["runs"][-1] if state["runs"] else {}
        return TrialObservation(final_text=last.get("final_text", ""), stop_reason=last.get("stop_reason") or "denied",
            events=tuple(captured), tool_calls=tuple(tool_observations(captured)), post_state={"bridge": state},
            usage=usage_summary(captured).get("usage_totals", {}),
            evidence=({"kind": "adapter_metadata", "bridge_evidence": state},))
    finally:
        if bridge is not None:
            await bridge.close()
        await host.stop()
