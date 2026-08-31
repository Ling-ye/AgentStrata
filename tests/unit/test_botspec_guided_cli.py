from __future__ import annotations

import json
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import stat
import sys
from unittest import mock

import yaml

from chatcopilot.botspec.cli import main as bot_cli_main
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.core.settings import load_local_env_values


def _new_starter(root: Path) -> Path:
    (root / "bots").mkdir()
    with mock.patch.dict(os.environ, {"CHATCOPILOT_HOME": str(root)}, clear=False):
        code = bot_cli_main(
            [
                "new",
                "my-assistant-qq",
                "--platform",
                "qq",
                "--preset",
                "starter",
                "--display-name",
                "我的助手",
            ]
        )
    assert code == 0
    return root / "bots" / "my-assistant-qq" / "bot.yaml"


def test_starter_scaffold_is_valid_and_contains_only_beginner_capabilities(tmp_path: Path) -> None:
    bot_yaml = _new_starter(tmp_path)
    spec = load_botspec(bot_yaml)

    assert not [item for item in validate_botspec(spec) if item.level == "error"]
    assert spec.llm.env_prefix == "CHATCOPILOT_CHAT"
    assert spec.agents.backend == "native"
    assert spec.tools.packs == ("workspace.read_write", "memory.chat")
    assert spec.tools.features == ("chat.file_uploads", "chat.private_workspace")
    assert spec.access.owner_only_project_access is True
    assert (bot_yaml.parent / "prompts" / "refusal-style.md").is_file()
    example = (bot_yaml.parent / "local.env.example").read_text(encoding="utf-8")
    payload = yaml.safe_load(bot_yaml.read_text(encoding="utf-8"))
    assert payload["tools"]["packs"] == ["workspace.read_write", "memory.chat"]
    assert "platform" not in payload
    assert payload["gateway"]["protocol_version"] == 1
    assert payload["channels"]["qq"] == {
        "type": "qq_personal",
        "provider": "onebot_v11",
        "channel_id": "qq",
        "endpoint_env": "CHATCOPILOT_QQ_ONEBOT_WS_URL",
        "access_token_env": "QQ_ACCESS_TOKEN",
        "account_env": "QQ_ACCOUNT",
        "mention_only_groups": True,
    }
    assert "cc_connect_config_dir" not in payload["deploy"]
    assert "YOUR_" not in example
    assert "CODEX" not in example
    assert "TAVILY" not in example
    assert "BRAVE" not in example
    assert "CHATCOPILOT_CC_CONNECT_BIN" not in example
    assert "QQ_WS_URL" not in example
    assert "QQ_AT_PROXY_URL" not in example
    assert "CHATCOPILOT_QQ_ONEBOT_WS_URL" in example
    assert "CHATCOPILOT_GATEWAY_TOKEN" in example
    assert "NAPCAT_SHM_SIZE" not in example


def test_minimal_scaffold_keeps_existing_shape(tmp_path: Path) -> None:
    (tmp_path / "bots").mkdir()
    with mock.patch.dict(os.environ, {"CHATCOPILOT_HOME": str(tmp_path)}, clear=False):
        code = bot_cli_main(["new", "minimal-bot", "--platform", "qq"])

    assert code == 0
    bot_dir = tmp_path / "bots" / "minimal-bot"
    payload = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    assert payload["tools"] == {"packs": [], "features": []}
    assert "access" not in payload
    assert not (bot_dir / "local.env.example").exists()
    assert not (bot_dir / "prompts" / "refusal-style.md").exists()


def test_starter_display_name_is_yaml_data_not_structure(tmp_path: Path) -> None:
    (tmp_path / "bots").mkdir()
    display_name = "助手: #一号"
    with mock.patch.dict(os.environ, {"CHATCOPILOT_HOME": str(tmp_path)}, clear=False):
        code = bot_cli_main(
            [
                "new",
                "safe-name-qq",
                "--platform",
                "qq",
                "--preset",
                "starter",
                "--display-name",
                display_name,
            ]
        )

    assert code == 0
    bot_yaml = tmp_path / "bots" / "safe-name-qq" / "bot.yaml"
    assert yaml.safe_load(bot_yaml.read_text(encoding="utf-8"))["display_name"] == display_name


def test_guided_configure_writes_mode_0600_and_doctor_json_is_secret_free(
    tmp_path: Path,
) -> None:
    bot_yaml = _new_starter(tmp_path)
    inputs = iter(
        (
            "https://llm.example.test/v1",
            "example-chat-model",
            "10001",
            "20002",
            "30003,30004",
        )
    )
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch.object(sys.stdout, "isatty", return_value=True),
        mock.patch("builtins.input", side_effect=lambda _prompt: next(inputs)),
        mock.patch("chatcopilot.botspec.cli.getpass.getpass", return_value="private-api-key"),
    ):
        code = bot_cli_main(["configure", "--bot", str(bot_yaml)])

    assert code == 0
    local_env = bot_yaml.parent / "local.env"
    values = load_local_env_values(local_env)
    assert stat.S_IMODE(local_env.stat().st_mode) == 0o600
    assert values["CHATCOPILOT_CHAT_BASE_URL"] == "https://llm.example.test/v1"
    assert values["CHATCOPILOT_CHAT_MODEL"] == "example-chat-model"
    assert values["CHATCOPILOT_CHAT_API_KEY"] == "private-api-key"
    assert values["QQ_ACCOUNT"] == "10001"
    assert values["CHATCOPILOT_ADD_OWNER_IDS"] == "20002"
    assert values["QQ_ALLOW_FROM"] == "20002"
    assert values["QQ_ALLOW_GROUPS"] == "30003,30004"
    assert 32 <= len(values["QQ_ACCESS_TOKEN"]) <= 128
    assert 32 <= len(values["CHATCOPILOT_GATEWAY_TOKEN"]) <= 128
    assert values["CHATCOPILOT_GATEWAY_TOKEN"] != values["QQ_ACCESS_TOKEN"]

    output = StringIO()
    with redirect_stdout(output):
        code = bot_cli_main(["doctor", "--bot", str(bot_yaml), "--json"])
    report = json.loads(output.getvalue())
    rendered = output.getvalue()
    assert code == 0
    assert report["schema_version"] == "agentstrata-deployment-check/v1"
    assert report["overall"] == "ready"
    assert {item["id"] for item in report["checks"]} >= {
        "llm_live_call",
        "qq_external_send",
        "qq_inbound_agent_roundtrip",
    }
    assert "private-api-key" not in rendered
    assert values["QQ_ACCESS_TOKEN"] not in rendered
    assert values["CHATCOPILOT_GATEWAY_TOKEN"] not in rendered


def test_invalid_guided_base_url_leaves_local_env_absent(tmp_path: Path) -> None:
    bot_yaml = _new_starter(tmp_path)
    inputs = iter(("http://remote.example.test/v1", "model", "10001", "20002", ""))
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch.object(sys.stdout, "isatty", return_value=True),
        mock.patch("builtins.input", side_effect=lambda _prompt: next(inputs)),
        mock.patch("chatcopilot.botspec.cli.getpass.getpass", return_value="private-api-key"),
    ):
        code = bot_cli_main(["configure", "--bot", str(bot_yaml)])

    assert code == 1
    assert not (bot_yaml.parent / "local.env").exists()


def test_guided_configure_rotates_invalid_existing_access_token(tmp_path: Path) -> None:
    bot_yaml = _new_starter(tmp_path)
    local_env = bot_yaml.parent / "local.env"
    local_env.write_text(
        "export CHATCOPILOT_CHAT_BASE_URL=https://llm.example.test/v1\n"
        "export CHATCOPILOT_CHAT_MODEL=test-model\n"
        "export CHATCOPILOT_CHAT_API_KEY=private-key\n"
        "export CHATCOPILOT_ADD_OWNER_IDS=20002\n"
        "export QQ_ACCOUNT=10001\n"
        "export QQ_ACCESS_TOKEN=weak-token\n"
        "export QQ_ALLOW_FROM=20002\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)
    inputs = iter(("", "", "", "", ""))
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch.object(sys.stdout, "isatty", return_value=True),
        mock.patch("builtins.input", side_effect=lambda _prompt: next(inputs)),
        mock.patch("chatcopilot.botspec.cli.getpass.getpass", return_value=""),
    ):
        code = bot_cli_main(["configure", "--bot", str(bot_yaml)])

    assert code == 0
    values = load_local_env_values(local_env)
    assert values["QQ_ACCESS_TOKEN"] != "weak-token"
    assert 32 <= len(values["QQ_ACCESS_TOKEN"]) <= 128


def test_nonstarter_configure_preserves_existing_qq_allowlist(tmp_path: Path) -> None:
    bot_yaml = _new_starter(tmp_path)
    payload = yaml.safe_load(bot_yaml.read_text(encoding="utf-8"))
    payload["tools"]["packs"] = ["workspace.read_write"]
    bot_yaml.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    local_env = bot_yaml.parent / "local.env"
    local_env.write_text(
        "export CHATCOPILOT_CHAT_BASE_URL=https://llm.example.test/v1\n"
        "export CHATCOPILOT_CHAT_MODEL=test-model\n"
        "export CHATCOPILOT_CHAT_API_KEY=private-key\n"
        "export CHATCOPILOT_ADD_OWNER_IDS=20002\n"
        "export QQ_ACCOUNT=10001\n"
        f"export QQ_ACCESS_TOKEN={'a' * 32}\n"
        "export QQ_ALLOW_FROM=10001,20002\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)
    inputs = iter(("", "", "", "", ""))
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch.object(sys.stdout, "isatty", return_value=True),
        mock.patch("builtins.input", side_effect=lambda _prompt: next(inputs)),
        mock.patch("chatcopilot.botspec.cli.getpass.getpass", return_value=""),
    ):
        code = bot_cli_main(["configure", "--bot", str(bot_yaml)])

    assert code == 0
    values = load_local_env_values(local_env)
    assert values["QQ_ALLOW_FROM"] == "10001,20002"
    assert values["QQ_ACCESS_TOKEN"] == "a" * 32


def test_configure_dry_run_never_creates_or_reads_local_env(tmp_path: Path) -> None:
    bot_yaml = _new_starter(tmp_path)
    local_env = bot_yaml.parent / "local.env"
    private_sentinel = "dry-run-private-sentinel"
    local_env.write_text(
        f"export CHATCOPILOT_CHAT_API_KEY={private_sentinel}\n",
        encoding="utf-8",
    )
    local_env.chmod(0o640)
    output = StringIO()
    with (
        redirect_stdout(output),
        mock.patch(
            "chatcopilot.botspec.cli.read_local_env_for_provision",
            side_effect=AssertionError("dry-run must not read local.env"),
        ),
    ):
        code = bot_cli_main(["configure", "--bot", str(bot_yaml), "--dry-run"])

    report = json.loads(output.getvalue())
    assert code == 0
    assert report["write"] is False
    assert local_env.read_text(encoding="utf-8").endswith(f"{private_sentinel}\n")
    assert private_sentinel not in output.getvalue()
    assert all("value" not in field for field in report["fields"])
    assert all(field["configured"] is False for field in report["fields"])
