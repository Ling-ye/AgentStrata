"""Real local Channel/Gateway/Application with deterministic Agent and fake iLink."""

import asyncio
import base64
from pathlib import Path
import socket

from test_application_actor_runtime import _FakeAgentRuntime

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.runtime import assemble_runtime_context
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.core.config import ChatConfig, LLMConfig
from chatcopilot.gateway import runtime as module


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2XcAAAAASUVORK5CYII="
)
ROOT = Path(__file__).resolve().parents[2]


def test_weixin_owner_turn_reuses_application_and_records_each_file_delivery(tmp_path, monkeypatch):
    agent = _FakeAgentRuntime()
    agent.subagent_default_model_client = None
    agent.close = lambda: None
    monkeypatch.setattr(
        module, "load_config", lambda **kwargs: ChatConfig(llm=LLMConfig(model="fixture-model"))
    )
    monkeypatch.setattr(module, "assemble_agent_runtime", lambda *args, **kwargs: agent)
    anchor = tmp_path / "private"
    anchor.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    wiki = tmp_path / "wiki"
    wiki.mkdir(mode=0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        "CHATCOPILOT_GATEWAY_PORT": str(port),
        "CHATCOPILOT_GATEWAY_TOKEN": "g" * 48,
        "CHATCOPILOT_GATEWAY_STATE_ROOT": str(anchor / "gateway"),
        "CHATCOPILOT_WORKSPACE_ROOT": str(workspace),
        "CHATCOPILOT_WIKI_ROOT": str(wiki),
        "CHATCOPILOT_ADD_OWNER_IDS": "weixin-owner",
        "WEIXIN_BOT_ID": "bot-id",
        "WEIXIN_USER_ID": "weixin-owner",
        "WEIXIN_BOT_TOKEN": "fake-token",
        "WEIXIN_API_BASE_URL": "https://ilinkai.weixin.qq.com",
    }
    runtime = assemble_runtime_context(load_botspec(ROOT / "bots/lingye-copilot-weixin/bot.yaml"))

    async def run():
        class Client:
            def __init__(self):
                self.updates = asyncio.Queue()
                self.sent = []
                self.first = True
                self.final = asyncio.Event()

            async def request(self, endpoint, body=None, **kwargs):
                if endpoint == "getupdates":
                    if self.first:
                        self.first = False
                        return {"ret": 0, "msgs": [], "get_updates_buf": "initial"}
                    return await self.updates.get()
                assert endpoint == "sendmessage"
                self.sent.append(body["msg"])
                item = body["msg"]["item_list"][0]
                if item.get("text_item", {}).get("text", "").startswith("reply:"):
                    self.final.set()
                return {"ret": 0}  # No fabricated provider message identity.

            async def upload(self, data, *, user_id, kind, name):
                assert user_id == "weixin-owner"
                return {"type": 2 if kind == "image" else 4, "fixture_name": name}

            async def close(self):
                pass

        client = Client()
        host = module.build_gateway_runtime_host(runtime, environ=environment, weixin_client=client)

        def send_files():
            creation = agent.sessions[0].creation
            assert creation["caller_role_hint"] == "owner"
            root = creation["host_policy"].scope.writable_roots[0]
            image = root / "picture.png"
            image.write_bytes(PNG)
            report = root / "report.txt"
            report.write_text("report")
            creation["file_sender"]([str(image), str(report)], "documents")

        agent.run_hook = send_files
        try:
            await host.start()
            assert host.ready
            await client.updates.put(
                {
                    "ret": 0,
                    "get_updates_buf": "next",
                    "msgs": [
                        {
                            "message_id": 1001,
                            "message_type": 1,
                            "from_user_id": "weixin-owner",
                            "to_user_id": "bot-id",
                            "context_token": "private-context",
                            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                        }
                    ],
                }
            )
            await asyncio.wait_for(client.final.wait(), 5)
            assert [entry["item_list"][0]["type"] for entry in client.sent] == [2, 4, 1, 1]
            assert all(entry["context_token"] == "private-context" for entry in client.sent)
            session = host.state_store.list_sessions()[0]
            assert session.account == ChannelAccountRef("weixin", "bot-id")
            assert session.conversation == ConversationRef("p2p", "weixin-owner")
            assert "private-context" not in repr(agent.sessions[0].tasks[0])
        finally:
            await host.stop()

    asyncio.run(run())
