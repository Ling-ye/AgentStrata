"""Regression coverage for durable large-image Gateway delivery.

The primary regression uses the production runtime composition:
``ToolExecutor -> file_sender_factory -> create_file_sender ->
ChannelRuntimeManager -> GatewayStateStore -> OneBot driver``.  The websocket
connection alone is a deterministic local fixture.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import ChannelsSpec, GatewaySpec, QQChannelSpec, WorkspaceSpec
from chatcopilot.channels.qq_onebot import OneBotForwardWebSocketDriver
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef, MessageSegment, OutboundEnvelope
from chatcopilot.contracts.identity import TurnIdentity
from chatcopilot.core.config import ChatConfig
from chatcopilot.evals.image_delivery_fixture import OneBotFixtureConnection
from chatcopilot.gateway import runtime as runtime_module
from chatcopilot.gateway.state_store import GatewayStateError, GatewayStateStore, OutboundConflict


_UNIT_TESTS = Path.cwd() / "tests" / "unit"
if str(_UNIT_TESTS) not in sys.path:
    sys.path.insert(0, str(_UNIT_TESTS))

from test_application_actor_runtime import _FakeAgentRuntime, _principal, _runtime
from test_gateway_runtime_host import _FakeServer, _environment


_LARGE_PNG_BYTES = 3_755_181


def _large_png() -> bytes:
    # The production validator recognizes PNG signatures.  This synthetic body
    # is deliberately large enough that its base64 OutboundEnvelope exceeds the
    # ordinary 1 MiB state-json limit, yet stays below OneBot's 8 MiB frame cap.
    return b"\x89PNG\r\n\x1a\n" + b"\0" * (_LARGE_PNG_BYTES - 8)


def _production_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _runtime(tmp_path)
    config.gateway, config.channels = GatewaySpec(), ChannelsSpec(qq=QQChannelSpec())
    config.spec.workspace = WorkspaceSpec()
    config.spec.llm.env_prefix = "CHATCOPILOT_TEST"
    config.bot_id = config.instance_id = "fixture"
    config.spec.context.wiki.enabled = False
    agent = _FakeAgentRuntime()
    agent.close = lambda: None
    connection = OneBotFixtureConnection()

    async def connect(_config):
        return connection

    def driver(cfg, on_event):
        return OneBotForwardWebSocketDriver(cfg, on_event, connection_factory=connect)

    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", lambda *args, **kwargs: agent)
    monkeypatch.setattr(runtime_module, "load_config", lambda **kwargs: ChatConfig())
    monkeypatch.setattr(runtime_module, "OneBotForwardWebSocketDriver", driver)
    monkeypatch.setattr(runtime_module, "GatewayWebSocketServer", _FakeServer)
    return runtime_module.build_gateway_runtime_host(config, environ=_environment(tmp_path)), agent, connection


def test_large_image_through_production_gateway_is_delivered_and_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid >1 MiB image reaches OneBot, receives an acknowledgement, and survives a state-store reopen."""
    host, agent, connection = _production_host(tmp_path, monkeypatch)
    image_bytes = _large_png()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    assert 1024 * 1024 < len(encoded) < 8 * 1024 * 1024

    async def scenario():
        await host.start()
        try:
            principal = _principal("20002")
            account = ChannelAccountRef("qq", "10001")
            conversation = ConversationRef("group", "30003")
            host.state_store.create_session(generation=host.generation, session_id="large-image-session", account=account, conversation=conversation)
            host.session_manager.create_session(generation=host.generation, session_id="large-image-session", account=account, conversation=conversation)
            host.state_store.begin_run(generation=host.generation, session_id="large-image-session", run_id="large-image-run", input_fingerprint="b" * 64)
            host.state_store.start_run(generation=host.generation, session_id="large-image-session", run_id="large-image-run")
            state = host.actor_factory.materialize(session_id="large-image-session", principal=principal,
                turn_identity=TurnIdentity(principal.conversation, principal.user_id))
            image_path = state.workspace.root / "xia-an.png"
            image_path.write_bytes(image_bytes)
            runtime_kwargs = agent.creations[-1]
            executor = ToolExecutor(tools=list(TOOLS), file_sender=runtime_kwargs["file_sender"],
                workspace_service=runtime_kwargs["workspace_service"], caller_role_hint="user",
                permission_filter=runtime_kwargs["permission_filter"])
            result = await asyncio.to_thread(executor.execute, "send_files_to_user", {"files": [str(image_path)]})
            # This is intentionally the first success assertion: on the frozen
            # baseline it is a behavior failure, rather than an unhandled helper
            # exception from later idempotency checks.
            assert result.ok, result.error

            actions = [action for action in connection.actions if action["action"] != "get_login_info"]
            assert len(actions) == 1
            image = next(segment for segment in actions[0]["params"]["message"] if segment["type"] == "image")
            assert base64.b64decode(image["data"]["file"].removeprefix("base64://")) == image_bytes
            records = host.state_store.find_outbound_deliveries(session_id="large-image-session", run_id="large-image-run")
            assert len(records) == 1
            record = records[0]
            assert record.state == "provider_acknowledged"
            assert record.provider_message_id
            assert record.envelope["segments"][0]["data"]["source"] == "base64://" + encoded
            return record
        finally:
            await host.stop()

    record = asyncio.run(scenario())
    reopened = GatewayStateStore(host.state_store.root)
    persisted = reopened.get_outbound(record.outbound_id)
    assert persisted is not None
    assert persisted.state == "provider_acknowledged"
    assert persisted.provider_message_id == record.provider_message_id
    assert persisted.envelope == record.envelope

    envelope = OutboundEnvelope(outbound_id=record.outbound_id,
        account=ChannelAccountRef(**record.envelope["account"]),
        conversation=ConversationRef(**record.envelope["conversation"]),
        segments=tuple(MessageSegment(**segment) for segment in record.envelope["segments"]),
        created_at=record.created_at, session_id=record.envelope["session_id"], run_id=record.envelope["run_id"])
    generation = reopened.acquire_writer_generation()
    assert reopened.enqueue_outbound(generation=generation, envelope=envelope) == persisted
    with pytest.raises(OutboundConflict):
        reopened.enqueue_outbound(generation=generation,
            envelope=replace(envelope, segments=envelope.segments + (MessageSegment(kind="text", text="changed"),)))


def test_outbound_capacity_is_finite_without_relaxing_other_state_json(tmp_path: Path) -> None:
    """The narrow outbox exception must leave generic JSON at 1 MiB and retain a finite outbox ceiling."""
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    with pytest.raises(GatewayStateError, match="byte limit"):
        store.append_event(generation=generation, event="channel.status", payload={"large": "x" * (1024 * 1024)})
    oversized = OutboundEnvelope(outbound_id="too-large-outbound", account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "30003"),
        segments=(MessageSegment(kind="image", data={"source": "base64://" + "A" * (11 * 1024 * 1024)}),),
        created_at=1.0, session_id="large-image-session", run_id="large-image-run")
    with pytest.raises(GatewayStateError, match="byte limit"):
        store.enqueue_outbound(generation=generation, envelope=oversized)
