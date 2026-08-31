from __future__ import annotations

from pathlib import Path

from console.control import operations
from console.control.instances import BotInstance
from chatcopilot.platforms.base import SecretSpec, SetupActionSpec


class FakeAdapter:
    name = "mock"
    adapter_id = "mock_acp"

    def required_secrets(self):
        return (
            SecretSpec("MOCK_TOKEN", required=True, description="Mock token"),
            SecretSpec("MOCK_REGION", required=False, default="local", description="Mock region"),
        )

    def setup_actions(self):
        return (
            SetupActionSpec(
                id="mock-setup",
                label="Mock setup",
                command=("mockctl", "{verb}", "{instance_id}", "{platform}"),
                allowed_verbs=("start", "status"),
            ),
        )


def test_provision_schema_is_adapter_driven(monkeypatch, tmp_path: Path) -> None:
    bot_dir = tmp_path / "bots" / "mock-bot"
    bot_dir.mkdir(parents=True)
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_text(
        "id: mock-bot\n"
        "display_name: MockBot\n"
        "platform:\n"
        "  type: mock\n"
        "  adapter: mock_acp\n"
        "llm:\n"
        "  chat:\n"
        "    env_prefix: CHATCOPILOT_MOCK\n"
        "prompts:\n"
        "  schema_version: 2\n"
        "  identity: prompts/identity.md\n"
        "  response_style: prompts/response-style.md\n"
        "  refusal_style: prompts/refusal-style.md\n"
        "tools:\n"
        "  packs: []\n"
        "  features: []\n"
        "agents:\n"
        "  backend: native\n",
        encoding="utf-8",
    )
    inst = BotInstance(
        instance_id="mock-bot",
        bot_spec=str(bot_yaml),
        display_name="MockBot",
        platform="mock",
    )
    monkeypatch.setattr(operations, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(operations, "get_adapter", lambda platform: FakeAdapter())

    schema = operations.provision_schema(inst)

    assert schema["platform"] == "mock"
    assert schema["adapter_id"] == "mock_acp"
    assert schema["schema_version"] == 2
    assert schema["common_fields"][0]["env_key"] == "CHATCOPILOT_MOCK_API_KEY"
    assert [field["env_key"] for field in schema["fields"]] == ["MOCK_TOKEN", "MOCK_REGION"]
    assert schema["setup_actions"] == [
        {
            "id": "mock-setup",
            "label": "Mock setup",
            "description": "",
            "guided_surface": "console",
            "default_verb": "start",
        }
    ]


def test_setup_action_execution_is_adapter_driven(monkeypatch) -> None:
    inst = BotInstance(
        instance_id="mock-bot",
        bot_spec="bots/mock-bot/bot.yaml",
        display_name="MockBot",
        platform="mock",
    )
    calls = []

    def fake_run_streaming(args, *, cwd=None, extra_env=None):
        calls.append((args, cwd, extra_env))
        yield "[OK] mock setup"
        yield "__EXIT__ 0"

    monkeypatch.setattr(operations, "get_adapter", lambda platform: FakeAdapter())
    monkeypatch.setattr(operations, "run_streaming", fake_run_streaming)
    monkeypatch.setattr(operations, "repo_root", lambda: operations.Path("/repo"))

    lines = list(operations.stream_setup_action(inst, "mock-setup", "status"))

    assert lines == [
        "[console] setup action mock-setup status: mock-bot",
        "[OK] mock setup",
        "__EXIT__ 0",
    ]
    assert calls[0][0] == ["mockctl", "status", "mock-bot", "mock"]
    assert operations.Path(calls[0][1]) == operations.Path("/repo")
    assert calls[0][2] is None
