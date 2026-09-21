from __future__ import annotations

from chatcopilot.botspec.model import ContextSpec

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import CodeLLMSpec, LLMSpec, ModelSpec
from chatcopilot.contracts.model_selection import (
    WorkerModelProfile,
    MODEL_SELECTION_SCOPE_ONCE,
)
from chatcopilot.core.config import RoutingConfig, LLMConfig
from dataclasses import replace
from chatcopilot.core.model_selection import (
    code_task_model_selection,
    default_worker_model_selection,
    validate_worker_model_selection,
)
from chatcopilot.middleware.acp.model_commands import handle_model_command
from chatcopilot.contracts.tools import (
    EXECUTION_USER_SERIAL_BACKGROUND,
)
from chatcopilot.middleware.runtime.jobs.submitter import submit_tool_job
from chatcopilot.core.workspace_runtime import Workspace


class _Session:
    def __init__(self, *, role: str = "owner") -> None:
        code = CodeLLMSpec(
            enabled=True,
            model="gpt-5.5",
            reasoning_effort="medium",
            profiles={
                "sol-high": WorkerModelProfile(
                    model="gpt-5.6-sol",
                    reasoning_effort="high",
                ),
            },
        )
        self.runtime = SimpleNamespace(
            spec=SimpleNamespace(context=ContextSpec(), llm=LLMSpec(code=code, chat=ModelSpec(profiles=code.profiles)))
        )
        self.main_model_route = LLMConfig(model="gpt-5.5", reasoning_effort="medium").model_route()
        self.role = SimpleNamespace(value=role)
        self.model_selection = None
        self.model_once = None

    def set_model_selection(self, selection) -> None:
        if selection.scope == MODEL_SELECTION_SCOPE_ONCE:
            self.model_once = selection
        else:
            self.model_selection = selection

    def clear_model_selection(self) -> None:
        self.model_selection = None
        self.model_once = None

    def effective_model_selection(self, default):
        return self.model_once or self.model_selection or default


def test_model_command_keeps_default_until_explicit_switch() -> None:
    session = _Session()

    reply = handle_model_command(session, "/model")

    assert reply is not None
    assert "gpt-5.5" in reply
    assert "medium" in reply
    assert session.model_selection is None
    assert session.model_once is None


def test_model_command_uses_effective_routing_config_over_raw_botspec() -> None:
    session = _Session()
    session.main_model_route = replace(session.main_model_route, model="gpt-5.6-terra")
    session.runtime.spec.llm = LLMSpec(chat=ModelSpec(profiles={
            "sol-max": WorkerModelProfile(
                model="gpt-5.6-sol",
                reasoning_effort="max",
            )
        }))

    status = handle_model_command(session, "/model")
    reply = handle_model_command(session, "/model sol-max")

    assert status is not None
    assert "gpt-5.6-terra" in status
    assert "sol-max" in status
    assert reply is not None
    assert session.model_selection is not None
    assert session.model_selection.reasoning_effort == "max"


def test_model_command_selects_allowlisted_profile_for_session() -> None:
    session = _Session()

    reply = handle_model_command(session, "/model sol-high")

    assert reply is not None
    assert "scope=session" in reply
    assert session.model_selection is not None
    assert session.model_selection.model == "gpt-5.6-sol"
    assert session.model_selection.reasoning_effort == "high"
    assert session.model_selection.profile == "sol-high"


def test_explicit_main_model_profile_can_select_once() -> None:
    session = _Session()

    reply = handle_model_command(session, "/model sol-high once")

    assert reply is not None
    assert "scope=once" in reply
    assert session.model_once is not None
    assert session.model_once.profile == "sol-high"
    assert session.model_once.scope == "once"


def test_natural_language_does_not_change_routing() -> None:
    session = _Session()

    reply = handle_model_command(
        session,
        "我希望让机器人换用5.6sol的high进行开发",
    )

    assert reply is None
    assert session.model_selection is None

    reset_reply = handle_model_command(session, "恢复默认开发模型")
    assert reset_reply is None
    assert session.model_selection is None


def test_unknown_model_does_not_change_existing_selection() -> None:
    session = _Session()
    handle_model_command(session, "/model sol-high")
    original = session.model_selection

    reply = handle_model_command(session, "/model gpt-9 impossible")

    assert reply is not None
    assert "设置未改变" in reply
    assert session.model_selection == original


def test_default_command_clears_session_and_once_overrides() -> None:
    session = _Session()
    handle_model_command(session, "/model sol-high")
    handle_model_command(session, "/model sol-high once")

    reply = handle_model_command(session, "/model default")

    assert reply is not None
    assert "gpt-5.5" in reply
    assert session.model_selection is None
    assert session.model_once is None


def test_disallowed_role_cannot_inspect_or_change_profiles() -> None:
    session = _Session(role="user")

    reply = handle_model_command(session, "/model sol-high")

    assert reply == "模型选择仅限 Owner。"
    assert session.model_selection is None


def test_worker_validates_frozen_profile_against_runtime_allowlist() -> None:
    config = RoutingConfig(
        code_model="gpt-5.5",
        code_reasoning_effort="medium",
        code_profiles={
            "sol-high": WorkerModelProfile(
                model="gpt-5.6-sol",
                reasoning_effort="high",
            )
        },
    )
    selection = validate_worker_model_selection(
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

    selection = validate_worker_model_selection(config, None)

    assert selection == default_worker_model_selection(config)


def test_code_task_selection_resolves_only_the_configured_profile() -> None:
    config = RoutingConfig(
        code_model="gpt-5.6-terra",
        code_reasoning_effort="medium",
        code_profiles={
            "sol-max": WorkerModelProfile(
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
