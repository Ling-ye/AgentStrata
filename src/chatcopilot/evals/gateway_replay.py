"""Isolated product replay through the production Gateway composition root."""
from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import json
import os
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
import socket
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from chatcopilot.application.workspaces import build_actor_workspace
from chatcopilot.channels.qq_onebot.codec import decode_inbound_message, parse_native_frame
from chatcopilot.channels.qq_onebot.resources import QqCdnResourceFetcher
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.evals.agent_case import CaseCapabilityUnavailable, LOCAL_PACKS, relative_path, validate_case
from chatcopilot.evals.case_images import CaseImages
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.execution_support import usage_summary
from chatcopilot.evals.frozen_agent_runtime import tool_observations
from chatcopilot.evals.image_delivery_fixture import ImageDeliveryFixture, OneBotFixtureConnection
from chatcopilot.evals.isolated_executor import _isolated_subagents
from chatcopilot.evals.models import EvalCase, TrialObservation
from chatcopilot.evals.trial_capture import execution_phase, record_turn
from chatcopilot.gateway.observation_queries import events as observation_events
from chatcopilot.gateway.runtime import build_gateway_runtime_host
from chatcopilot.gateway.coordinator import GatewayTurnCoordinatorError
from chatcopilot.tool_packs.catalog import resolve_tool_modules


def run(case: EvalCase, *, bot: str, workspace_root: Path) -> TrialObservation:
    declaration = validate_case(case.metadata["agent_case"])
    if declaration["context"]:
        raise CaseCapabilityUnavailable("三层回放不能把草案 context 当作已发生的会话或搜索结果")
    runtime = load_evaluation_runtime(bot)
    if runtime.gateway is None or runtime.channels.qq is None:
        raise CaseCapabilityUnavailable("三层回放需要可装配的 Gateway 和 QQ Channel")
    runtime = copy.deepcopy(runtime)
    packs = tuple(p for p in runtime.tool_packs if p in LOCAL_PACKS)
    known = {tool.name for module in resolve_tool_modules(packs)
             for tool in getattr(importlib.import_module(module), "TOOLS", ())}
    subagents = _isolated_subagents(runtime.subagents)
    runtime = replace(runtime, tool_packs=packs, mcp_servers=(), rag_sources=(), subagents=subagents,
                      exclude_tools=tuple(sorted(set(runtime.exclude_tools) | ((known | {"search_information"}) - set(declaration["allowed_tools"])))))
    # Project only machine-local resource roots; retain prompt, policy and model declarations.
    if runtime.spec.context.codebases.registry:
        raise CaseCapabilityUnavailable("三层回放尚未提供 codebase registry 的隔离资源")
    root = private_directory(workspace_root / "gateway-replay")
    workspaces, wiki, dev = (private_directory(root / name) for name in ("workspaces", "wiki", "dev"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    qq = runtime.channels.qq
    env = {runtime.gateway.port_env: str(port), runtime.gateway.token_env: "g" * 48,
           runtime.gateway.state_root_env: str(root / "state"),
           runtime.spec.workspace.root_env: str(workspaces), runtime.spec.context.wiki.root_env: str(wiki),
           runtime.spec.context.dev.root_env: str(dev), qq.endpoint_env: "ws://127.0.0.1:1",
           qq.access_token_env: "q" * 48, qq.account_env: "10001", "QQ_ALLOW_FROM": "20002",
           "CHATCOPILOT_ADD_OWNER_IDS": "20002" if declaration["role"] == "owner" else "",
           "CHATCOPILOT_ADD_ADMIN_IDS": "20002" if declaration["role"] == "admin" else "",
           "CHATCOPILOT_ADD_OWNER_NAMES": "", "CHATCOPILOT_ADD_ADMIN_NAMES": ""}
    if declaration.get("admission") == "denied":
        env["QQ_ALLOW_FROM"] = ""
    fixture = ImageDeliveryFixture(root, None, declaration)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(fixture.stack)
        fixture.install_http()
        responses = fixture.spec.get("http", {})

        def search_response(request, **_kwargs):
            row = responses.get(request.full_url)
            if row is None:
                # Search providers add their own query parameters; the frozen HTTP endpoint owns the response.
                url = urlsplit(request.full_url)
                row = responses.get(url._replace(query="").geturl())
            if row is None:
                raise OSError("No frozen HTTP response for this search endpoint")
            if row.get("status", 200) != 200:
                import urllib.error
                raise urllib.error.HTTPError(request.full_url, row["status"], "fixture", {}, None)
            return json.loads(base64.b64decode(row["body_base64"]))

        stack.enter_context(patch("chatcopilot.agent.search.providers._request_json", search_response))
        return asyncio.run(_execute(case, declaration, runtime, root, workspaces, env))


async def _execute(case, declaration, runtime, root, workspaces, env):
    connection = OneBotFixtureConnection(declaration.get("external_fixtures", {}).get("delivery", "acknowledged"))

    async def connect(_config):
        return connection

    resources = {}
    for ref in declaration.get("resources", []):
        if not case.metadata.get("image_root"):
            raise CaseCapabilityUnavailable("原图存储不可用")
        resources["https://fixture.invalid/input/" + ref["sha256"]] = (
            CaseImages(Path(case.metadata["image_root"])).read(ref), ref["media_type"])

    class Reader:
        async def read(self, url, *, host, addresses, max_bytes):
            if url not in resources:
                raise OSError("No frozen input resource")
            data, media_type = resources[url]
            if len(data) > max_bytes:
                raise ValueError("input resource exceeds limit")
            return data, media_type

    fetcher = QqCdnResourceFetcher(reader=Reader(), resolver=lambda *_: ("93.184.216.34",),
                                 allowed_domain_suffixes=("fixture.invalid",))
    host = build_gateway_runtime_host(runtime, environ={**os.environ, **env},
                                     onebot_connection_factory=connect, resource_fetcher=fetcher)
    kind = declaration["channel_kind"]
    principal = Principal("qq", "10001", ConversationIdentity("qq", "group" if kind == "group" else "p2p",
                          "30003" if kind == "group" else "20002"), "20002", Role(declaration["role"]), "fixture")
    workspace = build_actor_workspace(workspace_root=workspaces, principal=principal).workspace
    for name, text in declaration["fixtures"].items():
        path = workspace.root / relative_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    try:
        await host.start()
        message = ([{"type": "at", "data": {"qq": "10001"}}] if kind == "group" else [])
        message.append({"type": "text", "data": {"text": case.input}})
        message.extend({"type": "image", "data": {"file": url, "url": url}} for url in resources)
        raw = json.dumps({"post_type": "message", "message_type": kind, "self_id": "10001",
                          "message_id": "1", "group_id": "30003", "user_id": "20002",
                          "sender": {"user_id": "20002", "nickname": "Evaluation"}, "message": message})
        health = host.channels.health().channels[0]
        decoded = decode_inbound_message(parse_native_frame(raw, max_frame_bytes=1024 * 1024),
            account_id="10001", resource_ticket_ttl_seconds=300,
            connection_generation=health.connection_generation, observed_at=time.time())
        if decoded.event is None:
            raise ValueError("fixture input did not decode: " + decoded.code)
        record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": case.input, "completed": False})
        with execution_phase("runtime"):
            try:
                await host.channels.handle_inbound(decoded.event)
            except GatewayTurnCoordinatorError:
                # Only a recorded, expected admission denial is a valid early terminal.
                audit = host.state_store.list_authorization_decisions()
                if declaration.get("admission") != "denied" or not audit or audit[0].decision.allowed:
                    raise
        sessions = host.state_store.list_sessions()
        if not sessions:
            audit = host.state_store.list_authorization_decisions()
            if not audit or audit[0].decision.allowed:
                raise ValueError("missing Gateway execution evidence")
            replay = {"layers": ["gateway"], "admission": "denied", "production_delivery": False,
                      "decisions": [asdict(row) for row in audit], "receipts": []}
            record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": case.input,
                         "completed": True, "final_text": "", "stop_reason": "denied"})
            return TrialObservation(final_text="", stop_reason="denied",
                                    evidence=({"kind": "adapter_metadata", "runtime_replay": replay},))
        run = host.state_store.latest_run_for_session(sessions[0].session_id)
        if run is None or run.state not in {"completed", "failed", "aborted"}:
            raise ValueError("Gateway run did not reach a recorded terminal state")
        recorder = host.state_store.observation_recorder
        if recorder is None:
            raise ValueError("Gateway observation capture unavailable")
        captured, stages, cursor = [], [], 0
        while True:
            page = observation_events(recorder.store, run.run_id, after=cursor, limit=500)
            for row in page["observations"]:
                detail = recorder.store.body(run.run_id, row["body_ref"]) if row.get("body_ref") else None
                payload = (detail or {}).get("payload") or {}
                event = {"type": row["kind"], **row["data"], **payload}
                if row["kind"] == "RuntimeStageFinished":
                    stages.append({"operation": row["data"]["operation"], "layer": row["layer"],
                                   "status": row["status"], "span_id": row["data"].get("span_id")})
                elif row["kind"] == "ToolCatalogObserved":
                    event["phase"] = event.get("catalog_phase")
                captured.append(event)
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        layers = [layer for layer in ("gateway", "application", "agent") if any(s["layer"] == layer for s in stages)]
        outbox = host.state_store.find_outbound_deliveries(session_id=run.session_id, run_id=run.run_id)
        receipts = [{**asdict(receipt), "segments": [item["kind"] for item in outbound.envelope["segments"]],
                     "run_id": run.run_id, "session_id": run.session_id}
                    for outbound in outbox for receipt in host.state_store.delivery_receipts(outbound.outbound_id)]
        replay = {"layers": layers, "stages": stages, "run_id": run.run_id, "session_id": run.session_id,
                  "admission": "allowed", "production_delivery": False, "receipts": receipts}
        result = run.result or {}
        final = str(result.get("final_text", ""))
        stop = str(result.get("stop_reason", "")) or ("end_turn" if run.state == "completed" else "error")
        record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": case.input,
                     "completed": True, "final_text": final, "stop_reason": stop})
        state = {}
        for assertion in declaration["assertions"]:
            if "path" in assertion:
                path = workspace.root / relative_path(assertion["path"])
                if path.resolve() != path:
                    raise ValueError("produced resource escapes trial workspace")
                state[assertion["path"]] = {"exists": path.is_file(),
                    "text": path.read_text() if path.is_file() and path.stat().st_size <= 1024 * 1024 else None}
        return TrialObservation(final_text=final, stop_reason=stop, events=tuple(captured),
            tool_calls=tuple(tool_observations(captured)), post_state=state,
            usage=usage_summary(captured).get("usage_totals", {}),
            evidence=({"kind": "adapter_metadata", "runtime_replay": replay}, {"kind": "isolated_image_delivery", "production_delivery": False,
                                                                  "receipts": receipts}))
    finally:
        await host.stop()
