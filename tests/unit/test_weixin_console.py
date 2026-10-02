"""Weixin binding control and Console route regressions without external services."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import yaml

from chatcopilot.channels.weixin_ilink.login import WeixinLoginService
from chatcopilot.botspec.provisioning import read_local_env_for_provision
from chatcopilot.contracts.weixin import WeixinError
from console.backend.routes import bots, weixin
from console.backend.tasks import TaskManager
from console.control.weixin import WeixinControl


ROOT = Path(__file__).resolve().parents[2]


class LoginClient:
    def __init__(self, *, account="bot-id", user="weixin-owner"):
        self.account, self.user = account, user
        self.base_url = "https://ilinkai.weixin.qq.com"

    async def request(self, endpoint, body=None, **kwargs):
        if endpoint.startswith("get_bot_qrcode"):
            return {"qrcode": "qr-nonce", "qrcode_img_content": "https://example.test/scan"}
        return {
            "status": "confirmed",
            "bot_token": "fake-bot-token",
            "ilink_bot_id": self.account,
            "ilink_user_id": self.user,
            "baseurl": self.base_url,
        }

    async def close(self):
        pass


@pytest.fixture
def context(tmp_path, monkeypatch):
    from console.control import weixin as module

    repo = tmp_path / "repo"
    folder = repo / "bots/test-weixin"
    folder.mkdir(parents=True)
    data = yaml.safe_load((ROOT / "bots/lingye-copilot-weixin/bot.yaml").read_text())
    data["id"] = "test-weixin"
    data["deploy"]["instance_id"] = "test-weixin"
    path = folder / "bot.yaml"
    path.write_text(yaml.safe_dump(data))
    env = folder / "local.env"
    env.write_text(
        f"# keep comment\nUNRELATED_VALUE=preserved\nCHATCOPILOT_GATEWAY_STATE_ROOT={tmp_path / 'state'}\n"
    )
    env.chmod(0o600)
    instance = SimpleNamespace(instance_id="test-weixin", bot_spec=str(path), is_deployed=False)
    monkeypatch.setattr(module, "repo_root", lambda: repo)
    monkeypatch.setattr(module.operations, "status", lambda *args, **kwargs: {"running": False})
    return instance, env


def test_binding_saves_credentials_and_owner_atomically_preserving_other_configuration(context):
    async def run():
        instance, env = context
        service = WeixinLoginService(client_factory=LoginClient)
        control = WeixinControl(login_service=service)
        start = control.start(instance, TaskManager())
        session = service.get(instance.instance_id, start["login_id"])
        await session.task
        assert session.status == "confirmed"
        values = read_local_env_for_provision(env, allowed_parent=env.parent)
        assert values["WEIXIN_BOT_TOKEN"] == "fake-bot-token"
        assert values["WEIXIN_BOT_ID"] == "bot-id"
        assert values["CHATCOPILOT_ADD_OWNER_IDS"] == values["WEIXIN_USER_ID"] == "weixin-owner"
        assert values["UNRELATED_VALUE"] == "preserved" and "# keep comment" in env.read_text()
        assert len(values["CHATCOPILOT_GATEWAY_TOKEN"]) >= 32
        result = await control.status(instance)
        assert result["bound"] and not result["connected"]
        assert "fake-bot-token" not in json.dumps(result)

    asyncio.run(run())


def test_other_account_cannot_replace_an_existing_binding(context):
    async def run():
        instance, env = context
        env.write_text(
            env.read_text()
            + "WEIXIN_BOT_ID=original\nWEIXIN_USER_ID=original-user\nWEIXIN_BOT_TOKEN=original-token\n"
        )
        before = env.read_text()
        service = WeixinLoginService(client_factory=LoginClient)
        control = WeixinControl(login_service=service)
        result = control.start(instance, TaskManager())
        session = service.get(instance.instance_id, result["login_id"])
        await session.task
        assert session.status == "save_failed"
        assert session.error_code == "weixin_account_replacement_requires_new_instance"
        assert env.read_text() == before

    asyncio.run(run())


def test_running_instance_cannot_begin_binding(context, monkeypatch):
    from console.control import weixin as module

    instance, _ = context
    monkeypatch.setattr(module.operations, "status", lambda *args, **kwargs: {"running": True})
    with pytest.raises(WeixinError, match="instance_running"):
        WeixinControl(login_service=WeixinLoginService(client_factory=LoginClient)).start(
            instance, TaskManager()
        )


def test_cancellation_before_worker_starts_releases_binding_resources():
    async def run():
        finished = []
        service = WeixinLoginService(client_factory=LoginClient)
        result = service.start(
            "instance",
            lambda binding: pytest.fail("cancelled binding saved"),
            finish=lambda: finished.append(True),
        )
        await service.cancel("instance", result["login_id"])
        assert finished == [True] and not service.active("instance")

    asyncio.run(run())


def test_verified_gateway_health_is_required_for_connected_state(context, monkeypatch):
    async def run():
        from console.control import weixin as module

        instance, env = context
        env.write_text(
            env.read_text()
            + "WEIXIN_BOT_ID=bot-id\nWEIXIN_USER_ID=weixin-owner\nWEIXIN_BOT_TOKEN=fake-token\nCHATCOPILOT_GATEWAY_TOKEN="
            + "g" * 48
            + "\n"
        )
        monkeypatch.setattr(module.operations, "status", lambda *args, **kwargs: {"running": True})
        calls = []

        class Gateway:
            def __init__(self, config):
                assert config.client_id == "acp-edge"

            async def connect(self):
                calls.append("connect")

            async def request(self, method, params):
                assert method == "health"
                return SimpleNamespace(ready=False)

            async def close(self):
                calls.append("close")

        monkeypatch.setattr(module, "GatewayWebSocketClient", Gateway)
        result = await WeixinControl(
            login_service=WeixinLoginService(client_factory=LoginClient)
        ).status(instance)
        assert result["bound"] and not result["connected"] and result["connection_state"] == "error"
        assert calls == ["connect", "close"]

    asyncio.run(run())


def test_console_routes_coexist_and_enforce_local_same_origin_access(monkeypatch):
    app = FastAPI()
    app.state.tasks = TaskManager()

    class Control:
        async def status(self, instance):
            return {"bound": False, "connected": False}

        def start(self, instance, manager):
            return {"login_id": "fake-login", "status": "wait"}

    app.state.weixin = Control()
    instance = SimpleNamespace(instance_id="test-weixin")
    monkeypatch.setattr(weixin, "get_instance", lambda key: instance)
    monkeypatch.setattr(bots, "get_instance", lambda key: instance)
    monkeypatch.setattr(bots.operations, "control", lambda instance, verb: {"ok": True})
    app.include_router(weixin.router)
    app.include_router(bots.router)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/bots/test-weixin/channels/weixin/status").status_code == 200
        assert client.post("/api/bots/test-weixin/channels/weixin/login").status_code == 200
        assert (
            client.post(
                "/api/bots/test-weixin/channels/weixin/login",
                headers={"origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert client.post("/api/bots/test-weixin/start").status_code == 200
    with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 12345)) as client:
        assert client.post("/api/bots/test-weixin/channels/weixin/login").status_code == 403
