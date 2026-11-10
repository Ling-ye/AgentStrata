"""Preview security uses real host policy and Gateway dispatch, without live providers."""
import asyncio
from dataclasses import replace

import pytest

from chatcopilot.botspec.model import GatewayClientSpec, GatewaySpec
from chatcopilot.contracts.gateway_protocol import RequestFrame
from chatcopilot.contracts.identity import Role
from chatcopilot.gateway import GatewayDispatchError
from chatcopilot.host.runtime import parse_gateway_runtime_config
from tests.unit.test_gateway_runtime_host import _environment, _runtime as host_runtime
from tests.unit.test_gateway_application_dispatcher import _runtime, _client, _create, _Driver
from tests.unit.test_application_actor_runtime import _factory, _principal, _turn, ActorTurnExecutor
from tests.unit.test_application_tool_authorization import _tool


def test_preview_host_preserves_explicit_empty_tool_allowlist(tmp_path):
    runtime = host_runtime(wiki_enabled=False)
    runtime.gateway = GatewaySpec(clients=(GatewayClientSpec(
        "console-preview", "AGENTSTRATA_PREVIEW_TOKEN", "user", allowed_tools=()),))
    environment = _environment(tmp_path)
    environment["AGENTSTRATA_PREVIEW_TOKEN"] = "p" * 48
    config = parse_gateway_runtime_config(runtime, environment)
    assert config.client_roles["console-preview"] is Role.USER
    assert config.client_tool_allowlists["console-preview"] == frozenset()
    binding = next(c for c in config.credentials if c.client_id == "console-preview")
    assert binding.client_mode == "interactive"
    assert set(binding.scopes) == {"gateway.read", "chat.write", "chat.abort"}


def test_preview_client_run_never_enters_channel_delivery(tmp_path):
    async def exercise():
        state, _, _, _, coordinator, channels, _, dispatcher, actor = _runtime(tmp_path)
        driver = _Driver()
        channels.register(driver)
        await channels.start()
        await channels.activate()
        preview = replace(_client("console-preview"), client_mode="interactive")
        try:
            created = await _create(dispatcher, preview, "preview-session")
            session = created["session"]["sessionId"]
            with pytest.raises(GatewayDispatchError):
                await dispatcher.dispatch(RequestFrame("foreign", "sessions.get", {"sessionId": session}),
                                          client=_client("other-client"))
            sent = await dispatcher.dispatch(RequestFrame("send", "chat.send", {
                "sessionId": session, "segments": [{"kind": "text", "text": "hello"}]},
                idempotency_key="preview-turn"), client=preview)
            for _ in range(100):
                if state.get_run(sent["runId"]).state == "completed":
                    break
                await asyncio.sleep(0.01)
            assert state.get_run(sent["runId"]).state == "completed"
            assert actor.requests[0].principal.role is Role.USER
            assert actor.requests[0].principal.channel == "gateway"
            assert actor.requests[0].principal.user_id == "console-preview"
            assert driver.sent == []
            assert not state.find_outbound_deliveries(session_id=session, run_id=sent["runId"])
        finally:
            await coordinator.close()
            await channels.stop()
    asyncio.run(exercise())


def test_empty_preview_allowlist_blocks_tools_native_and_delivery_hooks(tmp_path):
    factory, agent, _ = _factory(tmp_path)
    principal = _principal("20002", role=Role.USER)
    factory._principal_tool_allowlists[(principal.channel, principal.account_id)] = frozenset()
    request = _turn(session_id="session-1", principal=principal,
                    canonical_text="send to QQ and write files", message_id="preview")
    executor = ActorTurnExecutor(factory)
    async def exercise():
        outcome = await executor.execute(request, on_event=lambda _: None)
        created = agent.creations[-1]
        policy = created["host_policy"]
        assert not policy.native_capabilities and not policy.extension_grants
        assert not policy.scope.native_write and not policy.scope.writable_roots
        assert created["file_sender"] is None and created["background_submitter"] is None
        for name in ("search_public", "write_workspace_file", "append_memory", "desktop_Click", "send_message"):
            assert created["permission_filter"](_tool(name, category="mcp", access="member")) is not None
        executor.discard_exchange(request, outcome)
    try:
        asyncio.run(exercise())
    finally:
        factory.close()
