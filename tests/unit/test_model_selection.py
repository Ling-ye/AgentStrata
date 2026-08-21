from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import CodeLLMSpec
from chatcopilot.contracts.model_selection import (
    CodeModelProfile,
    MODEL_SELECTION_SCOPE_ONCE,
)
from chatcopilot.core.config import RoutingConfig
from chatcopilot.core.model_selection import (
    code_task_model_selection,
    default_code_model_selection,
    validate_frozen_code_model_selection,
)
from chatcopilot.middleware.acp.model_commands import handle_model_command
from chatcopilot.external_tools.shared.tool_spec import (
    EXECUTION_USER_SERIAL_BACKGROUND,
)
from chatcopilot.middleware.runtime.jobs.submitter import submit_tool_job
from chatcopilot.middleware.runtime.workspace import Workspace


class _Session:
    def __init__(self, *, role: str = "owner") -> None:
        code = CodeLLMSpec(
            enabled=True,
            model="gpt-5.5",
            reasoning_effort="medium",
            profiles={
                "sol-high": CodeModelProfile(
                    model="gpt-5.6-sol",
                    reasoning_effort="high",
                ),
            },
        )
        self.runtime = SimpleNamespace(
            spec=SimpleNamespace(llm=SimpleNamespace(code=code))
        )
        self.role = SimpleNamespace(value=role)
        self.code_model_selection = None
        self.code_model_once = None

    def set_code_model_selection(self, selection) -> None:
        if selection.scope == MODEL_SELECTION_SCOPE_ONCE:
            self.code_model_once = selection
        else:
            self.code_model_selection = selection

    def clear_code_model_selection(self) -> None:
        self.code_model_selection = None
        self.code_model_once = None

    def effective_code_model_selection(self, default):
        return self.code_model_once or self.code_model_selection or default


def test_model_command_keeps_default_until_explicit_switch() -> None:
    session = _Session()

    reply = handle_model_command(session, "/model")

    assert reply is not None
    assert "model=gpt-5.5" in reply
    assert "reasoning=medium" in reply
    assert session.code_model_selection is None
    assert session.code_model_once is None


def test_model_command_uses_effective_routing_config_over_raw_botspec() -> None:
    session = _Session()
    session.routing_config = RoutingConfig(
        code_model="gpt-5.6-terra",
        code_reasoning_effort="medium",
        code_profiles={
            "sol-max": CodeModelProfile(
                model="gpt-5.6-sol",
                reasoning_effort="max",
            )
        },
        code_allowed_roles=("owner",),
    )

    status = handle_model_command(session, "/model")
    reply = handle_model_command(session, "/model code sol-max")

    assert status is not None
    assert "model=gpt-5.6-terra" in status
    assert "sol-max" in status
    assert reply is not None
    assert session.code_model_selection is not None
    assert session.code_model_selection.reasoning_effort == "max"


def test_model_command_selects_allowlisted_profile_for_session() -> None:
    session = _Session()

    reply = handle_model_command(session, "/model code sol-high")

    assert reply is not None
    assert "本会话后续代码任务" in reply
    assert session.code_model_selection is not None
    assert session.code_model_selection.model == "gpt-5.6-sol"
    assert session.code_model_selection.reasoning_effort == "high"
    assert session.code_model_selection.profile == "sol-high"


def test_natural_language_model_alias_can_select_once() -> None:
    session = _Session()

    reply = handle_model_command(session, "下一次改代码用5.6sol的high")

    assert reply is not None
    assert "仅下一次代码任务" in reply
    assert session.code_model_once is not None
    assert session.code_model_once.profile == "sol-high"
    assert session.code_model_once.scope == "once"


def test_requested_natural_language_phrase_selects_session_profile() -> None:
    session = _Session()

    reply = handle_model_command(
        session,
        "我希望让机器人换用5.6sol的high进行开发",
    )

    assert reply is not None
    assert session.code_model_selection is not None
    assert session.code_model_selection.profile == "sol-high"

    reset_reply = handle_model_command(session, "恢复默认开发模型")
    assert reset_reply is not None
    assert session.code_model_selection is None


def test_unknown_model_does_not_change_existing_selection() -> None:
    session = _Session()
    handle_model_command(session, "/model code sol-high")
    original = session.code_model_selection

    reply = handle_model_command(session, "/model code gpt-9 impossible")

    assert reply is not None
    assert "未修改当前设置" in reply
    assert session.code_model_selection == original


def test_default_command_clears_session_and_once_overrides() -> None:
    session = _Session()
    handle_model_command(session, "/model code sol-high")
    handle_model_command(session, "/model code sol-high once")

    reply = handle_model_command(session, "/model code default")

    assert reply is not None
    assert "model=gpt-5.5" in reply
    assert session.code_model_selection is None
    assert session.code_model_once is None


def test_disallowed_role_cannot_inspect_or_change_profiles() -> None:
    session = _Session(role="user")

    reply = handle_model_command(session, "/model code sol-high")

    assert reply == "当前角色无权查看或切换 Codex 开发模型。"
    assert session.code_model_selection is None


def test_worker_validates_frozen_profile_against_runtime_allowlist() -> None:
    config = RoutingConfig(
        code_model="gpt-5.5",
        code_reasoning_effort="medium",
        code_profiles={
            "sol-high": CodeModelProfile(
                model="gpt-5.6-sol",
                reasoning_effort="high",
            )
        },
    )
    selection = validate_frozen_code_model_selection(
        config,
        {
            "provider": "codex_cli",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "scope": "session",
            "source": "profile",
            "profile": "sol-high",
        },
    )

    assert selection.profile == "sol-high"
    assert selection.model == "gpt-5.6-sol"


def test_historical_job_without_selection_uses_current_default() -> None:
    config = RoutingConfig(
        code_model="gpt-5.5",
        code_reasoning_effort="medium",
    )

    selection = validate_frozen_code_model_selection(config, None)

    assert selection == default_code_model_selection(config)


def test_code_task_selection_resolves_only_the_configured_profile() -> None:
    config = RoutingConfig(
        code_model="gpt-5.6-terra",
        code_reasoning_effort="medium",
        code_profiles={
            "sol-max": CodeModelProfile(
                model="gpt-5.6-sol",
                reasoning_effort="max",
            )
        },
        code_task_profile="sol-max",
    )

    selection = code_task_model_selection(config)

    assert selection.profile == "sol-max"
    assert selection.model == "gpt-5.6-sol"
    assert selection.reasoning_effort == "max"


def test_botspec_validation_rejects_invalid_code_profiles() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "persona.md").write_text("demo\n", encoding="utf-8")
        bot_yaml = root / "bot.yaml"
        bot_yaml.write_text(
            "\n".join(
                [
                    "id: model-demo",
                    "display_name: Model Demo",
                    "platform:",
                    "  type: qq",
                    "  adapter: qq_acp",
                    "llm:",
                    "  code:",
                    "    reasoning_effort: impossible",
                    "    code_task_profile: missing-profile",
                    "    profiles:",
                    "      default:",
                    "        model: gpt-5.6-sol",
                    "        reasoning_effort: high",
                    "      empty-model:",
                    '        model: ""',
                    "        reasoning_effort: high",
                    "      bad-effort:",
                    "        model: gpt-5.6-sol",
                    "        reasoning_effort: impossible",
                    "prompts:",
                    "  schema_version: 2",
                    "  identity: persona.md",
                    "  response_style: persona.md",
                    "tools:",
                    "  packs: []",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        issues = validate_botspec(load_botspec(bot_yaml))

    error_fields = {issue.field for issue in issues if issue.level == "error"}
    assert "llm.code.reasoning_effort" in error_fields
    assert "llm.code.profiles.default" in error_fields
    assert "llm.code.profiles.empty-model" in error_fields
    assert "llm.code.profiles.bad-effort" in error_fields
    assert "llm.code.code_task_profile" in error_fields


def test_botspec_validation_requires_code_task_profile_for_dev_code_tasks() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "persona.md").write_text("demo\n", encoding="utf-8")
        bot_yaml = root / "bot.yaml"
        bot_yaml.write_text(
            "\n".join(
                [
                    "id: model-demo",
                    "display_name: Model Demo",
                    "platform:",
                    "  type: qq",
                    "  adapter: qq_acp",
                    "prompts:",
                    "  schema_version: 2",
                    "  identity: persona.md",
                    "  response_style: persona.md",
                    "tools:",
                    "  packs: [dev.code_tasks]",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        issues = validate_botspec(load_botspec(bot_yaml))

    assert any(
        issue.level == "error"
        and issue.field == "llm.code.code_task_profile"
        and "dev.code_tasks" in issue.message
        for issue in issues
    )


def test_submitter_persists_execution_profile_at_request_top_level() -> None:
    with TemporaryDirectory() as tmp:
        workspace = Workspace(
            root=Path(tmp) / "workspace",
            chat_kind="p2p",
            chat_id="chat-model-request",
            user_id="owner-model-request",
        ).ensure()
        profile = {
            "provider": "codex_cli",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "scope": "session",
            "source": "profile",
            "profile": "sol-high",
        }
        with mock.patch(
            "chatcopilot.middleware.runtime.jobs.submitter._spawn_worker"
        ):
            job = submit_tool_job(
                tool_name="run_coding_workflow",
                args={
                    "task": "fix src/app.py",
                    "execution_profile": profile,
                },
                execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
                workspace=workspace,
                session_id="sid-model-request",
            )

        request = json.loads(job.request_path.read_text(encoding="utf-8"))

    assert request["execution_profile"] == profile
    assert request["args"]["execution_profile"] == profile
