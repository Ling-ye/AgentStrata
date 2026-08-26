"""Owner-only ACP slash-command parsing and presentation helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from chatcopilot.contracts.identity import Role, role_value
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
    normalize_chat_kind,
)
from chatcopilot.middleware.acp.session_state import SessionState


OWNER_COMMAND_DENIED_TEXT = "斜杠指令仅限 Owner 使用。"

_SLASH_COMMAND_RE = re.compile(
    r"\A\s*/(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?=\s|\Z)"
)
_MAX_COMMAND_NAME_CHARS = 64
_SAFE_ACTIVE_STATES = {
    "active": "运行中",
    "activating": "启动中",
    "deactivating": "停止中",
    "failed": "失败",
    "inactive": "未运行",
    "maintenance": "维护中",
    "reloading": "重载中",
    "unknown": "未知",
}
_SAFE_RESTART_STATES = {
    "accepted": "已受理",
    "failed": "失败",
    "idle": "空闲",
    "pending": "等待执行",
    "restarting": "重启中",
    "scheduled": "已安排",
    "succeeded": "成功",
    "unknown": "未知",
}


@dataclass(frozen=True)
class ParsedSlashCommand:
    """A leading slash command with its unmodified, trimmed argument text."""

    name: str
    arguments: str


@dataclass(frozen=True)
class OperatorCommandResult:
    """A side-effect-free routing decision for one parsed slash command."""

    command: ParsedSlashCommand
    action: Literal["reply", "restart", "passthrough"]
    text: str | None = None
    code: str = ""


@dataclass(frozen=True)
class _CommandHelp:
    name: str
    usage: str
    summary: str
    handler: Literal["operator", "restart", "passthrough"]


COMMAND_CATALOG = (
    _CommandHelp("help", "/help", "显示当前实例可用的指令", "operator"),
    _CommandHelp("state", "/state", "查看当前会话与宿主状态", "operator"),
    _CommandHelp("restart", "/restart", "安全请求重启当前机器人实例", "restart"),
    _CommandHelp("model", "/model code ...", "查看或切换本会话的 Codex 开发模型", "passthrough"),
    _CommandHelp("task", "/task [job_id]", "查看代码任务状态", "passthrough"),
    _CommandHelp("cancel", "/cancel [job_id]", "取消代码任务", "passthrough"),
    _CommandHelp(
        "persona",
        "/persona ...",
        "查看或管理持续人格；确认提案使用 /persona confirm",
        "passthrough",
    ),
    _CommandHelp("debug", "/debug on|off|status", "查看或切换本会话调试模式", "passthrough"),
)
_COMMAND_BY_NAME = {item.name: item for item in COMMAND_CATALOG}
if len(_COMMAND_BY_NAME) != len(COMMAND_CATALOG):
    raise RuntimeError("operator command catalog contains duplicate names")

OPERATOR_COMMANDS = frozenset(
    item.name for item in COMMAND_CATALOG if item.handler != "passthrough"
)
LEGACY_PASSTHROUGH_COMMANDS = frozenset(
    item.name for item in COMMAND_CATALOG if item.handler == "passthrough"
)


def parse_slash_command(text: str) -> ParsedSlashCommand | None:
    """Parse only the first ``/name`` token; filesystem paths are not commands."""

    raw = str(text or "")
    match = _SLASH_COMMAND_RE.match(raw)
    if match is None:
        return None
    name = match.group("name").lower()
    if len(name) > _MAX_COMMAND_NAME_CHARS:
        name = "__invalid__"
    return ParsedSlashCommand(
        name=name,
        arguments=raw[match.end() :].strip(),
    )


def handle_operator_command(
    session: SessionState,
    text: str,
    *,
    host_state: Mapping[str, object] | None = None,
    restart_available: bool = False,
    supports_debug: bool | None = None,
) -> OperatorCommandResult | None:
    """Return a reply, restart request, or legacy-handler passthrough decision."""

    command = parse_slash_command(text)
    if command is None:
        return None
    if not _is_owner(session):
        return OperatorCommandResult(
            command=command,
            action="reply",
            text=OWNER_COMMAND_DENIED_TEXT,
            code="owner_command_required",
        )
    definition = _COMMAND_BY_NAME.get(command.name)
    if definition is None:
        return OperatorCommandResult(
            command=command,
            action="reply",
            text="未知斜杠指令。发送 /help 查看当前可用指令。",
            code="unknown_command",
        )
    if definition.handler != "passthrough" and command.arguments:
        return OperatorCommandResult(
            command=command,
            action="reply",
            text=f"用法：/{command.name}",
            code="invalid_arguments",
        )
    if not _command_available(
        session,
        definition,
        restart_available=restart_available,
        supports_debug=supports_debug,
    ):
        return OperatorCommandResult(
            command=command,
            action="reply",
            text=f"当前机器人未启用 /{command.name} 指令。",
            code="command_unavailable",
        )
    if definition.handler == "passthrough":
        return OperatorCommandResult(
            command=command,
            action="passthrough",
            code="legacy_passthrough",
        )
    if command.name == "help":
        return OperatorCommandResult(
            command=command,
            action="reply",
            text=format_help(
                session,
                restart_available=restart_available,
                supports_debug=supports_debug,
            ),
            code="help",
        )
    if command.name == "state":
        return OperatorCommandResult(
            command=command,
            action="reply",
            text=format_state(session, host_state=host_state),
            code="state",
        )
    return OperatorCommandResult(
        command=command,
        action="restart",
        code="restart_requested",
    )


def format_help(
    session: SessionState,
    *,
    restart_available: bool = False,
    supports_debug: bool | None = None,
) -> str:
    """Format the commands enabled by the current runtime capability snapshot."""

    if not _is_owner(session):
        return OWNER_COMMAND_DENIED_TEXT
    commands = [
        item
        for item in COMMAND_CATALOG
        if _command_available(
            session,
            item,
            restart_available=restart_available,
            supports_debug=supports_debug,
        )
    ]

    lines = ["可用斜杠指令（仅限 Owner）："]
    lines.extend(f"- {item.usage}：{item.summary}" for item in commands)
    return "\n".join(lines)


def format_state(
    session: SessionState,
    *,
    host_state: Mapping[str, object] | None = None,
) -> str:
    """Format bounded session and host status without identifiers or paths."""

    if not _is_owner(session):
        return OWNER_COMMAND_DENIED_TEXT

    runtime = getattr(session, "runtime", None)
    workspace = getattr(session, "workspace", None)
    chat_kind = normalize_chat_kind(getattr(workspace, "chat_kind", None))
    chat_label = {"group": "群聊", "p2p": "私聊"}.get(chat_kind or "", "未知")
    scope_label = {
        WORKSPACE_SCOPE_ACTOR: "独立会话",
        WORKSPACE_SCOPE_GROUP_SHARED: "群共享",
    }.get(str(getattr(workspace, "scope", "") or ""), "未知")
    assistant_mode = _safe_enum_value(
        getattr(session, "assistant_mode", None),
        allowed={"general", "performance"},
    )
    platform = _safe_identifier(getattr(runtime, "platform_type", None))
    backend = _safe_identifier(getattr(runtime, "agent_backend", None))
    display_name = _safe_display_name(getattr(runtime, "display_name", None))
    instance_id = _safe_identifier(getattr(runtime, "instance_id", None))
    message_count = _message_count(session)
    chat_model = _safe_model_label(getattr(session, "llm_model", None))
    code_profile = _selected_code_profile(session)

    lines = [
        "当前机器人状态：",
        f"- 机器人：{display_name}（实例 {instance_id}）",
        f"- 运行包络：平台 {platform}；Agent backend {backend}",
        f"- 模型：chat {chat_model}；Codex profile {code_profile}",
        f"- 会话：{chat_label}；角色 Owner；业务模式 {assistant_mode}",
        (
            f"- Workspace：{scope_label}；"
            f"{'已初始化' if bool(getattr(session, 'is_workspace_materialized', False)) else '未初始化'}"
        ),
        (
            f"- Agent：{'已初始化' if bool(getattr(session, 'is_materialized', False)) else '未初始化'}；"
            f"消息数 {message_count}；调试模式 {'on' if bool(getattr(session, 'debug_mode', False)) else 'off'}"
        ),
    ]
    lines.extend(_format_host_state(host_state))
    return "\n".join(lines)


def _is_owner(session: SessionState) -> bool:
    return role_value(getattr(session, "role", Role.USER)) == Role.OWNER.value


def _code_model_available(session: SessionState) -> bool:
    runtime = getattr(session, "runtime", None)
    spec = getattr(runtime, "spec", None)
    llm = getattr(spec, "llm", None)
    code = getattr(llm, "code", None)
    if code is None or not bool(getattr(code, "enabled", False)):
        return False
    allowed_roles = {
        str(item).strip().lower()
        for item in getattr(code, "allowed_roles", ()) or ()
        if str(item).strip()
    }
    return not allowed_roles or Role.OWNER.value in allowed_roles


def _legacy_command_available(
    session: SessionState,
    name: str,
    *,
    supports_debug: bool | None,
) -> bool:
    if name == "model":
        return _code_model_available(session)
    if name == "debug":
        return _debug_available(session, supports_debug=supports_debug)
    runtime = getattr(session, "runtime", None)
    packs = {
        str(item).strip()
        for item in getattr(runtime, "tool_packs", ()) or ()
        if str(item).strip()
    }
    if name in {"task", "cancel"}:
        return "dev.code_tasks" in packs
    if name == "persona":
        return "persona.control" in packs
    return False


def _command_available(
    session: SessionState,
    command: _CommandHelp,
    *,
    restart_available: bool,
    supports_debug: bool | None,
) -> bool:
    if command.handler == "operator":
        return True
    if command.handler == "restart":
        return restart_available
    return _legacy_command_available(
        session,
        command.name,
        supports_debug=supports_debug,
    )


def _debug_available(
    session: SessionState,
    *,
    supports_debug: bool | None,
) -> bool:
    if supports_debug is not None:
        return supports_debug
    runtime = getattr(session, "runtime", None)
    platform_type = str(getattr(runtime, "platform_type", "") or "").strip()
    if not platform_type:
        return False
    try:
        from chatcopilot.platforms import router as platform_router

        return platform_router.supports_role_matrix(platform_type)
    except (LookupError, RuntimeError, ValueError):
        return False


def _message_count(session: SessionState) -> int:
    counter = getattr(session, "message_count", None)
    if not callable(counter):
        return 0
    try:
        value = int(counter())
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _safe_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or re.fullmatch(r"[a-z0-9_.-]{1,64}", text) is None:
        return "unknown"
    return text


def _safe_display_name(value: object) -> str:
    text = " ".join(
        "".join(char for char in str(value or "") if char.isprintable()).split()
    )
    if not text:
        return "未命名机器人"
    return text[:80]


def _safe_model_label(value: object) -> str:
    text = str(value or "").strip()
    if not text or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}", text) is None:
        return "unknown"
    return text


def _selected_code_profile(session: SessionState) -> str:
    selection = getattr(session, "code_model_once", None) or getattr(
        session,
        "code_model_selection",
        None,
    )
    profile = _safe_identifier(getattr(selection, "profile", None))
    if profile != "unknown":
        return profile
    source = str(getattr(selection, "source", "") or "").strip().lower()
    return "default" if not selection or source == "default" else "unknown"


def _safe_enum_value(value: object, *, allowed: set[str]) -> str:
    text = role_value(value)
    return text if text in allowed else "unknown"


def _format_host_state(host_state: Mapping[str, object] | None) -> list[str]:
    state = host_state or {}
    lines: list[str] = []

    active_state = str(state.get("active_state") or "").strip().lower()
    load_state = str(state.get("load_state") or "").strip().lower()
    sub_state = str(state.get("sub_state") or "").strip().lower()
    running = state.get("running")
    if isinstance(running, bool):
        lines.append(f"- 宿主进程：{'运行中' if running else '未运行'}")
    elif active_state in _SAFE_ACTIVE_STATES:
        lines.append(f"- 宿主进程：{_SAFE_ACTIVE_STATES[active_state]}")

    systemd_available = state.get("systemd_available")
    registered = state.get("registered")
    if isinstance(systemd_available, bool) or isinstance(registered, bool):
        systemd_label = (
            "可用"
            if systemd_available is True
            else "不可用"
            if systemd_available is False
            else "未知"
        )
        registered_label = (
            "已注册"
            if registered is True
            else "未注册"
            if registered is False
            else "未知"
        )
        lines.append(f"- 宿主管理：systemd {systemd_label}；实例 {registered_label}")

    if load_state or active_state or sub_state:
        safe_load = load_state if load_state in {"loaded", "not-found", "masked"} else "unknown"
        safe_active = active_state if active_state in _SAFE_ACTIVE_STATES else "unknown"
        safe_sub = (
            sub_state
            if sub_state
            in {
                "auto-restart",
                "dead",
                "exited",
                "failed",
                "running",
                "start",
                "stop",
            }
            else "unknown"
        )
        lines.append(
            f"- systemd 状态：load={safe_load}；active={safe_active}；sub={safe_sub}"
        )

    ws_connected = state.get("ws_connected")
    if isinstance(ws_connected, bool):
        lines.append(f"- 平台连接：{'已连接' if ws_connected else '未连接'}")

    restart_state = str(state.get("restart_state") or "").strip().lower()
    restart_pending = state.get("restart_pending")
    if restart_state in _SAFE_RESTART_STATES:
        lines.append(f"- 重启状态：{_SAFE_RESTART_STATES[restart_state]}")
    elif isinstance(restart_pending, bool):
        lines.append(f"- 重启状态：{'等待执行' if restart_pending else '空闲'}")

    if not lines:
        lines.append("- 宿主状态：当前未提供")
    return lines


__all__ = [
    "LEGACY_PASSTHROUGH_COMMANDS",
    "OPERATOR_COMMANDS",
    "COMMAND_CATALOG",
    "OWNER_COMMAND_DENIED_TEXT",
    "OperatorCommandResult",
    "ParsedSlashCommand",
    "format_help",
    "format_state",
    "handle_operator_command",
    "parse_slash_command",
]
