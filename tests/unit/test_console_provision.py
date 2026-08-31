import subprocess
from pathlib import Path
import re
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from chatcopilot.core.settings import load_local_env_values
from chatcopilot.platforms.base import SecretSpec
from console.backend.routes import bots as bot_routes
from console.control import operations
from console.control.instances import BotInstance

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def canonical_console_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(operations, "repo_root", lambda: tmp_path)
    return tmp_path


def _inst(tmp_path: Path, *, platform: str, env_prefix: str = "CHATCOPILOT_CHAT") -> BotInstance:
    instance_id = "sample-qq" if platform == "qq" else "sample-bot"
    bot_dir = tmp_path / "bots" / instance_id
    bot_dir.mkdir(parents=True)
    bot_yaml = bot_dir / "bot.yaml"
    transport = (
        [
            "gateway:",
            "  protocol_version: 1",
            "  host: 127.0.0.1",
            "  port_env: CHATCOPILOT_GATEWAY_PORT",
            "  token_env: CHATCOPILOT_GATEWAY_TOKEN",
            "  state_root_env: CHATCOPILOT_GATEWAY_STATE_ROOT",
            "channels:",
            "  qq:",
            "    type: qq_personal",
            "    provider: onebot_v11",
            "    channel_id: qq",
            "    endpoint_env: CHATCOPILOT_QQ_ONEBOT_WS_URL",
            "    access_token_env: QQ_ACCESS_TOKEN",
            "    account_env: QQ_ACCOUNT",
            "    mention_only_groups: true",
        ]
        if platform == "qq"
        else ["platform:", f"  type: {platform}", f"  adapter: {platform}_acp"]
    )
    bot_yaml.write_text(
        "\n".join(
            [
                f"id: {instance_id}",
                f"display_name: {instance_id}",
                *transport,
                "llm:",
                "  chat:",
                f"    env_prefix: {env_prefix}",
                "prompts:",
                "  schema_version: 2",
                "  identity: prompts/identity.md",
                "  response_style: prompts/response-style.md",
                "  refusal_style: prompts/refusal-style.md",
                "tools:",
                "  packs: []",
                "  features: []",
                "agents:",
                "  backend: native",
                "workspace:",
                "  root_env: CHATCOPILOT_WORKSPACE_ROOT",
                "deploy:",
                "  target: wsl2",
                f"  instance_id: {instance_id}",
                f"  env_file: ~/.chatcopilot-{instance_id}.env",
                "access:",
                "  owner_only_project_access: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return BotInstance(
        instance_id=instance_id,
        bot_spec=str(bot_yaml),
        display_name=instance_id,
        platform=platform,
        runtime_kind="gateway" if platform == "qq" else "legacy_edge",
        wsl_home=f"/srv/test/AgentStrata-{instance_id}",
        workspace_root=f"/srv/test/agentstrata-workspaces/{instance_id}",
        log_dir=f"/srv/test/agentstrata-logs/{instance_id}",
        env_file=str(tmp_path / f".chatcopilot-{instance_id}.env"),
        cc_connect_config_dir=f"/srv/test/.agentstrata-runtime/{instance_id}/.cc-connect",
        cc_home=f"/srv/test/.agentstrata-runtime/{instance_id}",
        project_name=f"agentstrata-{instance_id}",
    )


def _starter_inst(tmp_path: Path) -> BotInstance:
    inst = _inst(tmp_path, platform="qq")
    bot_yaml = Path(inst.bot_spec)
    bot_yaml.write_text(
        "id: sample-qq\n"
        "display_name: sample-qq\n"
        "gateway:\n"
        "  protocol_version: 1\n"
        "  host: 127.0.0.1\n"
        "  port_env: CHATCOPILOT_GATEWAY_PORT\n"
        "  token_env: CHATCOPILOT_GATEWAY_TOKEN\n"
        "  state_root_env: CHATCOPILOT_GATEWAY_STATE_ROOT\n"
        "channels:\n"
        "  qq:\n"
        "    type: qq_personal\n"
        "    provider: onebot_v11\n"
        "    channel_id: qq\n"
        "    endpoint_env: CHATCOPILOT_QQ_ONEBOT_WS_URL\n"
        "    access_token_env: QQ_ACCESS_TOKEN\n"
        "    account_env: QQ_ACCOUNT\n"
        "    mention_only_groups: true\n"
        "llm:\n"
        "  chat:\n"
        "    env_prefix: CHATCOPILOT_CHAT\n"
        "prompts:\n"
        "  schema_version: 2\n"
        "  identity: prompts/identity.md\n"
        "  response_style: prompts/response-style.md\n"
        "  refusal_style: prompts/refusal-style.md\n"
        "tools:\n"
        "  packs:\n"
        "    - workspace.read_write\n"
        "    - memory.chat\n"
        "  features:\n"
        "    - chat.file_uploads\n"
        "    - chat.private_workspace\n"
        "context:\n"
        "  memory_store:\n"
        "    provider: markdown\n"
        "    namespace: sample-qq\n"
        "agents:\n"
        "  backend: native\n"
        "  presets: []\n"
        "workspace:\n"
        "  root_env: CHATCOPILOT_WORKSPACE_ROOT\n"
        "deploy:\n"
        "  target: wsl2\n"
        "  instance_id: sample-qq\n"
        "  wsl_home: ~/ChatCopilot-sample-qq\n"
        "  workspace_root: ~/chatcopilot-workspaces/sample-qq\n"
        "  log_dir: ~/chatcopilot-logs/sample-qq\n"
        "  env_file: ~/.chatcopilot-sample-qq.env\n"
        "  project_name: chatcopilot-sample-qq\n"
        "access:\n"
        "  owner_only_project_access: true\n",
        encoding="utf-8",
    )
    prompts = bot_yaml.parent / "prompts"
    prompts.mkdir()
    for name in ("identity.md", "response-style.md", "refusal-style.md"):
        (prompts / name).write_text(f"# {name}\n", encoding="utf-8")
    return inst


def _successful_runtime():
    return patch(
        "console.control.operations.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="[OK]", stderr=""),
    )


def test_qq_provision_uses_botspec_llm_prefix_and_secret_free_receipt(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq", env_prefix="CHATCOPILOT_DEMO")

    with _successful_runtime() as run:
        res = operations.write_instance_env(
            inst,
            {
                "chat_api_key": "sk-test",
                "QQ_ACCOUNT": "123456789",
                    "CHATCOPILOT_QQ_ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "QQ_ACCESS_TOKEN": "a" * 32,
                "QQ_ALLOW_FROM": "987654321",
            },
        )

    assert res["ok"] is True
    values = load_local_env_values(Path(str(res["local_env_file"])))
    assert values["CHATCOPILOT_DEMO_API_KEY"] == "sk-test"
    assert values["QQ_ACCOUNT"] == "123456789"
    assert values["QQ_ACCESS_TOKEN"] == "a" * 32
    assert "CHATCOPILOT_CHAT_API_KEY" not in values
    assert "sk-test" not in str(res["receipt"])
    assert "a" * 32 not in str(res["receipt"])
    assert res["receipt"]["committed"] is True
    assert "CHATCOPILOT_DEMO_API_KEY" in res["written_keys"]
    assert "QQ_ACCOUNT" in res["written_keys"]
    provision_cmd = run.call_args.args[0]
    assert provision_cmd[-2:] == ["--bot", inst.bot_spec]


def test_qq_provision_rejects_missing_qq_account_without_writing(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq")
    res = operations.write_instance_env(inst, {"chat_api_key": "sk-test"})

    assert res["ok"] is False
    assert "qq_account" in str(res["error"]).lower()
    assert not (Path(inst.bot_spec).parent / "local.env").exists()


def test_starter_console_generates_token_and_defaults_owner_admission(
    canonical_console_repo: Path,
) -> None:
    inst = _starter_inst(canonical_console_repo)

    with _successful_runtime():
        res = operations.write_instance_env(
            inst,
            {
                "chat_api_key": "sk-test",
                "chat_base_url": "https://example.invalid/v1",
                "chat_model": "test-model",
                "add_owner_ids": "987654321",
                "qq_account": "123456789",
            },
        )

    assert res["ok"] is True
    values = load_local_env_values(Path(str(res["local_env_file"])))
    access_token = values["QQ_ACCESS_TOKEN"]
    gateway_token = values["CHATCOPILOT_GATEWAY_TOKEN"]
    assert 32 <= len(access_token) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_-]+", access_token)
    assert 32 <= len(gateway_token) <= 128
    assert gateway_token != access_token
    assert values["QQ_ALLOW_FROM"] == "987654321"
    assert access_token not in str(res)
    assert "QQ_ACCESS_TOKEN" in res["written_keys"]
    assert "CHATCOPILOT_GATEWAY_TOKEN" in res["written_keys"]
    assert "QQ_ALLOW_FROM" in res["written_keys"]

    schema = operations.provision_schema(inst)
    platform_by_key = {field["env_key"]: field for field in schema["fields"]}
    assert platform_by_key["QQ_ACCESS_TOKEN"]["host_generated"] is True
    assert platform_by_key["CHATCOPILOT_GATEWAY_TOKEN"]["host_generated"] is True
    assert platform_by_key["QQ_ACCESS_TOKEN"]["configured"] is True
    assert platform_by_key["QQ_ALLOW_FROM"]["required"] is False


def test_starter_console_rejects_explicit_host_generated_token(
    canonical_console_repo: Path,
) -> None:
    inst = _starter_inst(canonical_console_repo)
    submitted_token = "K" * 43

    with patch("console.control.operations.subprocess.run") as run:
        res = operations.write_instance_env(
            inst,
            {
                "chat_api_key": "sk-test",
                "chat_base_url": "https://example.invalid/v1",
                "chat_model": "test-model",
                "add_owner_ids": "987654321",
                "qq_account": "123456789",
                "qq_access_token": submitted_token,
            },
        )

    assert res == {
        "ok": False,
        "stage": "local_env",
        "error": "host_generated_field_must_not_be_submitted:qq_access_token",
    }
    assert submitted_token not in str(res)
    assert not (Path(inst.bot_spec).parent / "local.env").exists()
    run.assert_not_called()


def test_qq_provision_rejects_weak_token_without_echoing_or_writing(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq")
    weak_token = 'weak"token'
    with patch("console.control.operations.subprocess.run") as run:
        res = operations.write_instance_env(
            inst,
            {
                "chat_api_key": "sk-test",
                "QQ_ACCOUNT": "123456789",
                "QQ_ACCESS_TOKEN": weak_token,
            },
        )

    assert res["ok"] is False
    assert "qq_access_token_invalid" in str(res["error"])
    assert weak_token not in str(res["error"])
    assert not (Path(inst.bot_spec).parent / "local.env").exists()
    run.assert_not_called()


def test_provision_rejects_unknown_flat_body_field(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq")

    res = operations.write_instance_env(inst, {"CHATCOPILOT_LEGACY_API_KEY": "sk-old"})

    assert res["ok"] is False
    assert res["error"] == "unknown_provision_field"
    assert "sk-old" not in str(res["error"])


def test_runtime_generation_failure_keeps_committed_secret_free_receipt(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq")
    with patch(
        "console.control.operations.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="runtime failed", stderr=""),
    ):
        res = operations.write_instance_env(
            inst,
            {
                "chat_api_key": "sk-test",
                "qq_account": "123456789",
                "qq_access_token": "a" * 32,
            },
        )

    assert res["ok"] is False
    assert res["stage"] == "runtime_env"
    assert res["receipt"]["committed"] is True
    assert "sk-test" not in str(res)
    assert "a" * 32 not in str(res)


def test_route_rejects_nested_provision_body_before_operation(tmp_path: Path, monkeypatch) -> None:
    inst = _inst(tmp_path, platform="qq")
    monkeypatch.setattr(bot_routes, "get_instance", lambda _instance_id: inst)
    monkeypatch.setattr(
        operations,
        "write_instance_env",
        lambda *_args, **_kwargs: pytest.fail("operation must not receive a nested body"),
    )

    with pytest.raises(HTTPException) as caught:
        bot_routes.bot_provision_env("sample-qq", {"chat_api_key": {"nested": "value"}})

    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "provision_body_must_be_flat_strings"


def test_empty_secret_preserves_existing_value(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq", env_prefix="CHATCOPILOT_DEMO")
    local_env = Path(inst.bot_spec).parent / "local.env"
    local_env.write_text(
        "# keep this comment\n"
        "export CHATCOPILOT_DEMO_API_KEY='existing-key'\n"
        "export QQ_ACCOUNT='123456789'\n"
        f"export QQ_ACCESS_TOKEN='{'a' * 32}'\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)

    with _successful_runtime():
        res = operations.write_instance_env(inst, {"chat_api_key": ""})

    assert res["ok"] is True
    assert "chat_api_key" in res["receipt"]["preserved_fields"]
    assert "existing-key" in local_env.read_text(encoding="utf-8")
    assert "# keep this comment" in local_env.read_text(encoding="utf-8")


def test_feishu_provision_still_requires_platform_credentials(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="feishu")
    res = operations.write_instance_env(inst, {"chat_api_key": "sk-test"})

    assert res["ok"] is False
    assert "feishu_app_id" in str(res["error"]).lower()

    res = operations.write_instance_env(
        inst,
        {"chat_api_key": "sk-test", "feishu_app_id": "cli-test"},
    )
    assert res["ok"] is False
    assert "feishu_app_secret" in str(res["error"]).lower()


def test_provision_schema_v2_is_dynamic_and_reports_configured_fields(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="qq", env_prefix="CHATCOPILOT_DEMO")
    local_env = Path(inst.bot_spec).parent / "local.env"
    local_env.write_text("export CHATCOPILOT_DEMO_API_KEY='configured'\n", encoding="utf-8")
    local_env.chmod(0o600)

    schema = operations.provision_schema(inst)

    assert schema["schema_version"] == 2
    assert schema["bot_id"] == "sample-qq"
    common_by_key = {field["env_key"]: field for field in schema["common_fields"]}
    assert common_by_key["CHATCOPILOT_DEMO_API_KEY"] == {
        "field": "chat_api_key",
        "env_key": "CHATCOPILOT_DEMO_API_KEY",
        "label": "LLM API Key",
        "group": "llm",
        "required": True,
        "secret": True,
        "default": None,
        "description": "OpenAI-compatible API key",
        "configured": True,
        "host_generated": False,
    }
    platform_by_key = {field["env_key"]: field for field in schema["fields"]}
    assert platform_by_key["QQ_ACCOUNT"]["required"] is True
    assert schema["setup_actions"][0]["guided_surface"] == "terminal"
    assert schema["setup_actions"][0]["default_verb"] == "bootstrap"


def test_builtin_qq_schema_uses_real_prefix_and_code_worker_requirement() -> None:
    bot_yaml = ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"
    inst = BotInstance(
        instance_id="lingye-copilot-qq",
        bot_spec=str(bot_yaml),
        display_name="Lingye",
        platform="qq",
    )

    schema = operations.provision_schema(inst)

    common_keys = {field["env_key"] for field in schema["common_fields"]}
    assert "CHATCOPILOT_LINGYE_API_KEY" in common_keys
    assert "CHATCOPILOT_CHAT_API_KEY" not in common_keys
    assert schema["requires_code_worker"] is True


def test_fake_platform_schema_and_write_need_no_console_branch(
    canonical_console_repo: Path,
) -> None:
    inst = _inst(canonical_console_repo, platform="fake")

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

        def validate_runtime_env(self, _env):
            return ()

    with patch("console.control.operations.get_adapter", return_value=FakeAdapter()):
        schema = operations.provision_schema(inst)
        assert [field["env_key"] for field in schema["fields"]] == ["FAKE_TOKEN", "FAKE_OPTION"]

        with _successful_runtime():
            res = operations.write_instance_env(
                inst,
                {
                    "chat_api_key": "sk-test",
                    "fake_token": "tok",
                    "FAKE_OPTION": "opt",
                },
            )

    assert res["ok"] is True
    values = load_local_env_values(Path(str(res["local_env_file"])))
    assert values["CHATCOPILOT_CHAT_API_KEY"] == "sk-test"
    assert values["FAKE_TOKEN"] == "tok"
    assert values["FAKE_OPTION"] == "opt"
