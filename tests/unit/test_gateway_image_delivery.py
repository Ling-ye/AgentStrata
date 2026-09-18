"""Production composition sends workspace images through the real Channel and OneBot driver."""
import asyncio
import base64

from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import ChannelsSpec, GatewaySpec, WorkspaceSpec, QQChannelSpec
from chatcopilot.channels.qq_onebot import OneBotForwardWebSocketDriver
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.identity import TurnIdentity
from chatcopilot.core.config import ChatConfig
from chatcopilot.evals.image_delivery_fixture import OneBotFixtureConnection
from chatcopilot.gateway import runtime as runtime_module
from test_application_actor_runtime import _runtime, _FakeAgentRuntime, _principal
from test_gateway_runtime_host import _environment, _FakeServer
from test_image_delivery_fixture import PNG


def test_production_factory_injects_a_causally_connected_sender(tmp_path, monkeypatch):
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
    monkeypatch.setattr(runtime_module, "assemble_agent_runtime", lambda *a, **kw: agent)
    monkeypatch.setattr(runtime_module, "load_config", lambda **kw: ChatConfig())
    monkeypatch.setattr(runtime_module, "OneBotForwardWebSocketDriver", driver)
    monkeypatch.setattr(runtime_module, "GatewayWebSocketServer", _FakeServer)
    host = runtime_module.build_gateway_runtime_host(config, environ=_environment(tmp_path))

    async def scenario():
        await host.start()
        try:
            principal = _principal("20002")
            account, conversation = ChannelAccountRef("qq", "10001"), ConversationRef("group", "30003")
            host.state_store.create_session(generation=host.generation, session_id="session-1",
                                            account=account, conversation=conversation)
            host.session_manager.create_session(generation=host.generation, session_id="session-1",
                                                account=account, conversation=conversation)
            host.state_store.begin_run(generation=host.generation, session_id="session-1", run_id="run-image",
                                       input_fingerprint="a" * 64)
            host.state_store.start_run(generation=host.generation, session_id="session-1", run_id="run-image")
            state = host.actor_factory.materialize(session_id="session-1", principal=principal,
                turn_identity=TurnIdentity(principal.conversation, principal.user_id))
            kwargs = agent.creations[-1]
            target = state.workspace.root / "image.png"
            target.write_bytes(PNG)
            executor = ToolExecutor(tools=list(TOOLS), file_sender=kwargs["file_sender"],
                workspace_service=kwargs["workspace_service"], caller_role_hint="user",
                permission_filter=kwargs["permission_filter"])
            result = await asyncio.to_thread(executor.execute, "send_files_to_user", {"files": [str(target)]})
            assert result.ok, result.error
            outbound = [a for a in connection.actions if a["action"] != "get_login_info"]
            assert len(outbound) == 1
            image = next(s for s in outbound[0]["params"]["message"] if s["type"] == "image")
            assert base64.b64decode(image["data"]["file"].removeprefix("base64://")) == PNG
            assert outbound[0]["params"]["group_id"] == "30003"
        finally:
            await host.stop()
    asyncio.run(scenario())
