import base64
import subprocess
from pathlib import Path
from unittest.mock import patch

from console.control import operations
from console.control.instances import BotInstance
from chatcopilot.platforms.base import SecretSpec


def _inst(tmp_path: Path, *, platform: str) -> BotInstance:
    instance_id = "lingye-copilot-qq" if platform == "qq" else "sample-bot"
    return BotInstance(
        instance_id=instance_id,
        bot_spec=f"bots/{instance_id}/bot.yaml",
        display_name=instance_id,
        platform=platform,
        wsl_home=f"/srv/test/ChatCopilot-{instance_id}",
        workspace_root=f"/srv/test/chatcopilot-workspaces/{instance_id}",
        log_dir=f"/srv/test/chatcopilot-logs/{instance_id}",
        env_file=str(tmp_path / f".chatcopilot-{instance_id}.env"),
        cc_connect_config_dir=f"/srv/test/.chatcopilot-runtime/{instance_id}/.cc-connect",
        cc_home=f"/srv/test/.chatcopilot-runtime/{instance_id}",
        project_name=f"chatcopilot-{instance_id}",
    )


def _decode_payload(payload: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in payload.splitlines():
        key, encoded = line.split("=", 1)
        out[key] = base64.b64decode(encoded).decode("utf-8")
    return out


def test_qq_provision_requires_chat_key_account_and_token_but_not_feishu(tmp_path: Path) -> None:
    inst = _inst(tmp_path, platform="qq")

    with patch("console.control.operations.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="[OK]", stderr="")
        res = operations.write_instance_env(
            inst,
            {
                "CHATCOPILOT_CHAT_API_KEY": "sk-test",
                "QQ_ACCOUNT": "123456789",
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_ACCESS_TOKEN": "a" * 32,
                "QQ_ALLOW_FROM": "987654321",
            },
        )

    assert res["ok"] is True
    local_env_file = str(res["local_env_file"]).replace("\\", "/")
    assert local_env_file.endswith("bots/lingye-copilot-qq/local.env")
    payload = _decode_payload(run.call_args_list[0].kwargs["input"])
    assert payload["CHATCOPILOT_CHAT_API_KEY"] == "sk-test"
    assert payload["QQ_ACCOUNT"] == "123456789"
    assert payload["QQ_WS_URL"] == "ws://127.0.0.1:3001"
    assert payload["QQ_ACCESS_TOKEN"] == "a" * 32
    assert payload["QQ_ALLOW_FROM"] == "987654321"
    assert "FEISHU_APP_ID" not in payload
    assert "FEISHU_APP_SECRET" not in payload
    provision_cmd = run.call_args_list[1].args[0]
    assert provision_cmd[-2:] == ["--bot", "bots/lingye-copilot-qq/bot.yaml"]


def test_qq_provision_rejects_missing_qq_account(tmp_path: Path) -> None:
    res = operations.write_instance_env(
        _inst(tmp_path, platform="qq"),
        {"CHATCOPILOT_CHAT_API_KEY": "sk-test"},
    )

    assert res["ok"] is False
    assert "QQ_ACCOUNT" in str(res["error"])


def test_qq_provision_rejects_weak_token_before_writing_secret(tmp_path: Path) -> None:
    weak_token = 'weak"token'
    with patch("console.control.operations.subprocess.run") as run:
        res = operations.write_instance_env(
            _inst(tmp_path, platform="qq"),
            {
                "CHATCOPILOT_CHAT_API_KEY": "sk-test",
                "QQ_ACCOUNT": "123456789",
                "QQ_ACCESS_TOKEN": weak_token,
            },
        )

    assert res["ok"] is False
    assert "qq_access_token_invalid" in str(res["error"])
    assert weak_token not in str(res["error"])
    run.assert_not_called()


def test_feishu_provision_still_requires_feishu_credentials(tmp_path: Path) -> None:
    res = operations.write_instance_env(
        _inst(tmp_path, platform="feishu"),
        {"CHATCOPILOT_CHAT_API_KEY": "sk-test"},
    )

    assert res["ok"] is False
    assert "FEISHU_APP_ID" in str(res["error"])
    assert "FEISHU_APP_SECRET" in str(res["error"])


def test_feishu_provision_writes_existing_fields(tmp_path: Path) -> None:
    inst = _inst(tmp_path, platform="feishu")

    with patch("console.control.operations.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="[OK]", stderr="")
        res = operations.write_instance_env(
            inst,
            {
                "CHATCOPILOT_CHAT_API_KEY": "sk-test",
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret",
                "CHATCOPILOT_ADD_OWNER_IDS": "ou_owner",
                "TAVILY_API_KEY": "tvly-test",
            },
        )

    assert res["ok"] is True
    local_env_file = str(res["local_env_file"]).replace("\\", "/")
    assert local_env_file.endswith("bots/sample-bot/local.env")
    payload = _decode_payload(run.call_args_list[0].kwargs["input"])
    assert payload["FEISHU_APP_ID"] == "cli_test"
    assert payload["FEISHU_APP_SECRET"] == "secret"
    assert payload["CHATCOPILOT_ADD_OWNER_IDS"] == "ou_owner"
    assert payload["TAVILY_API_KEY"] == "tvly-test"
    assert "QQ_ACCOUNT" not in payload
    provision_cmd = run.call_args_list[1].args[0]
    assert provision_cmd[-2:] == ["--bot", "bots/sample-bot/bot.yaml"]


def test_provision_schema_comes_from_platform_adapter(tmp_path: Path) -> None:
    schema = operations.provision_schema(_inst(tmp_path, platform="qq"))

    field_by_key = {field["env_key"]: field for field in schema["fields"]}
    assert field_by_key["QQ_ACCOUNT"]["required"] is True
    assert "QQ_WS_URL" in field_by_key
    assert schema["setup_actions"][0]["id"] == "qq-gateway"


def test_fake_platform_schema_writes_without_console_branch(tmp_path: Path) -> None:
    inst = _inst(tmp_path, platform="fake")

    class FakeAdapter:
        name = "fake"
        adapter_id = "fake_acp"

        def required_secrets(self):
            return (
                SecretSpec("FAKE_TOKEN", required=True, description="fake token"),
                SecretSpec("FAKE_OPTION", required=False, description="fake option"),
            )

        def setup_actions(self):
            return ()

    with patch("console.control.operations.get_adapter", return_value=FakeAdapter()):
        schema = operations.provision_schema(inst)
        assert [field["env_key"] for field in schema["fields"]] == ["FAKE_TOKEN", "FAKE_OPTION"]

        with patch("console.control.operations.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="[OK]", stderr="")
            res = operations.write_instance_env(
                inst,
                {
                    "chat_api_key": "sk-test",
                    "fake_token": "tok",
                    "FAKE_OPTION": "opt",
                },
            )

    assert res["ok"] is True
    payload = _decode_payload(run.call_args_list[0].kwargs["input"])
    assert payload["CHATCOPILOT_CHAT_API_KEY"] == "sk-test"
    assert payload["FAKE_TOKEN"] == "tok"
    assert payload["FAKE_OPTION"] == "opt"
