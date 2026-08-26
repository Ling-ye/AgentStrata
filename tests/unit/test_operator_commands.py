from __future__ import annotations

from types import SimpleNamespace

import pytest

from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
)
from chatcopilot.middleware.acp.operator_commands import (
    COMMAND_CATALOG,
    OWNER_COMMAND_DENIED_TEXT,
    format_help,
    format_state,
    handle_operator_command,
    parse_slash_command,
)

_TARGET_UNIT = "chatcopilot" + "@" + "qq-bot.service"


def _session(
    *,
    role: Role = Role.OWNER,
    packs: tuple[str, ...] = ("dev.code_tasks", "persona.control"),
    code_enabled: bool = True,
    code_roles: tuple[str, ...] = ("owner",),
    scope: str = WORKSPACE_SCOPE_ACTOR,
) -> SimpleNamespace:
    runtime = SimpleNamespace(
        instance_id="qq-bot",
        display_name="Test Bot",
        platform_type="qq",
        agent_backend="codex",
        tool_packs=packs,
        spec=SimpleNamespace(
            llm=SimpleNamespace(
                code=SimpleNamespace(
                    enabled=code_enabled,
                    allowed_roles=code_roles,
                )
            )
        ),
    )
    return SimpleNamespace(
        role=role,
        runtime=runtime,
        workspace=SimpleNamespace(
            scope=scope,
            chat_kind="group" if scope == WORKSPACE_SCOPE_GROUP_SHARED else "p2p",
            chat_id="raw-chat-id",
            user_id="raw-user-id",
            root="/private/workspace/path",
        ),
        assistant_mode=SimpleNamespace(value="performance"),
        debug_mode=False,
        is_workspace_materialized=False,
        is_materialized=False,
        session_id="raw-session-id",
        execution_session_id="raw-executor-id",
        llm_model="chat-model",
        code_model_once=None,
        code_model_selection=SimpleNamespace(profile="sol-max", source="profile"),
        message_count=lambda: 4,
    )


def test_command_catalog_names_and_usages_are_unique() -> None:
    names = [item.name for item in COMMAND_CATALOG]
    usages = [item.usage for item in COMMAND_CATALOG]

    assert len(names) == len(set(names))
    assert len(usages) == len(set(usages))


@pytest.mark.parametrize(
    ("text", "name", "arguments"),
    (
        ("/help", "help", ""),
        ("  /STATE  ", "state", ""),
        ("\n/restart now ", "restart", "now"),
        ("/model code sol-max once", "model", "code sol-max once"),
        ("/persona confirm", "persona", "confirm"),
        ("/unknown-name x Y", "unknown-name", "x Y"),
    ),
)
def test_parse_slash_command(text: str, name: str, arguments: str) -> None:
    parsed = parse_slash_command(text)

    assert parsed is not None
    assert parsed.name == name
    assert parsed.arguments == arguments


@pytest.mark.parametrize(
    "text",
    (
        "",
        "hello /help",
        "//help",
        "/tmp/report.txt",
        "/help/more",
        "https://example.test/help",
        "／help",
    ),
)
def test_parse_slash_command_does_not_classify_paths_or_inline_text(text: str) -> None:
    assert parse_slash_command(text) is None


def test_overlong_command_name_stays_classified_but_is_bounded() -> None:
    parsed = parse_slash_command("/" + "a" * 10_000)

    assert parsed is not None
    assert parsed.name == "__invalid__"
    assert parsed.arguments == ""


@pytest.mark.parametrize(
    "text",
    (
        "/help",
        "/state",
        "/restart",
        "/debug status",
        "/model code",
        "/task",
        "/cancel",
        "/persona confirm",
        "/unknown",
    ),
)
def test_every_parsed_slash_command_rechecks_owner(text: str) -> None:
    decision = handle_operator_command(
        _session(role=Role.USER),
        text,
        restart_available=True,
        supports_debug=True,
    )

    assert decision is not None
    assert decision.action == "reply"
    assert decision.code == "owner_command_required"
    assert decision.text == OWNER_COMMAND_DENIED_TEXT


def test_unknown_owner_command_is_not_passed_to_the_agent() -> None:
    decision = handle_operator_command(_session(), "/does-not-exist")

    assert decision is not None
    assert decision.action == "reply"
    assert decision.code == "unknown_command"
    assert decision.text == "未知斜杠指令。发送 /help 查看当前可用指令。"


def test_legacy_passthrough_requires_the_same_runtime_capability_as_help() -> None:
    session = _session(packs=(), code_enabled=False)

    for name in ("model", "task", "cancel", "persona", "debug"):
        decision = handle_operator_command(
            session,
            f"/{name}",
            supports_debug=False,
        )
        assert decision is not None
        assert decision.action == "reply"
        assert decision.code == "command_unavailable"

    help_text = format_help(session, restart_available=False, supports_debug=False)
    assert help_text.splitlines() == [
        "可用斜杠指令（仅限 Owner）：",
        "- /help：显示当前实例可用的指令",
        "- /state：查看当前会话与宿主状态",
    ]


def test_help_uses_capability_filtered_command_catalog() -> None:
    session = _session()

    text = format_help(session, restart_available=True, supports_debug=True)

    for command in (
        "/help",
        "/state",
        "/restart",
        "/model code ...",
        "/task [job_id]",
        "/cancel [job_id]",
        "/persona ...",
        "/debug on|off|status",
    ):
        assert command in text


def test_invalid_operator_arguments_return_exact_usage_without_action() -> None:
    for text in ("/help now", "/state verbose", "/restart other-bot"):
        decision = handle_operator_command(
            _session(),
            text,
            restart_available=True,
        )
        assert decision is not None
        assert decision.action == "reply"
        assert decision.code == "invalid_arguments"
        assert decision.text == f"用法：/{text.split()[0][1:]}"


def test_state_is_bounded_and_excludes_actor_paths_units_and_pid() -> None:
    session = _session(scope=WORKSPACE_SCOPE_GROUP_SHARED)
    state = format_state(
        session,
        host_state={
            "status_known": True,
            "systemd_available": True,
            "registered": True,
            "running": True,
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
            "unit": _TARGET_UNIT,
            "main_pid": 987654,
            "logs": "raw-host-log-marker",
        },
    )

    assert "实例 qq-bot" in state
    assert "群聊" in state
    assert "群共享" in state
    assert "Codex profile sol-max" in state
    assert "load=loaded；active=active；sub=running" in state
    for protected in (
        "raw-chat-id",
        "raw-user-id",
        "raw-session-id",
        "raw-executor-id",
        "/private/workspace/path",
        _TARGET_UNIT,
        "987654",
        "raw-host-log-marker",
    ):
        assert protected not in state


def test_state_and_help_fail_closed_for_non_owner() -> None:
    session = _session(role=Role.ADMIN)

    assert format_help(session) == OWNER_COMMAND_DENIED_TEXT
    assert format_state(session) == OWNER_COMMAND_DENIED_TEXT
