"""Owner-only assistant persona tools backed by trusted persistent state."""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.agent.tools.workspace_context import resolve_persistent_state, resolve_workspace
from chatcopilot.contracts.persistent_state import (
    PERSONA_SCOPES,
    PersistentConversationState,
    has_meaningful_persona,
)
from chatcopilot.contracts.tools import ToolContext
from chatcopilot.external_tools.shared.spec_helpers import require_arg
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef


def _persistent_state(ctx: ToolContext | None) -> PersistentConversationState:
    state = ctx.persistent_state if ctx is not None else None
    return state if state is not None else resolve_persistent_state()


def _require_owner(ctx: ToolContext | None) -> None:
    role = str(ctx.caller_role if ctx is not None else "").strip().lower()
    if not role:
        from chatcopilot.core.caller_context import get_caller_role_hint

        role = get_caller_role_hint()
    if role != "owner":
        raise PermissionError("人格配置仅限 Owner 管理。")


def _default_scope(ctx: ToolContext | None) -> str:
    workspace = ctx.workspace if ctx is not None else None
    workspace = workspace or resolve_workspace(create=True)
    return "group" if (workspace.chat_kind or "").strip().lower() == "group" else "user"


def _resolve_scope(args: Dict[str, Any], ctx: ToolContext | None) -> str:
    raw = args.get("scope")
    return str(raw).strip().lower() if raw is not None and str(raw).strip() else _default_scope(ctx)


def _meaningful_persona(text: str) -> str:
    stripped = (text or "").strip()
    return stripped if has_meaningful_persona(stripped) else ""


def _handler_persona_show(
    args: Dict[str, Any], ctx: ToolContext | None = None
) -> HandlerResult:
    del args
    _require_owner(ctx)
    layers = [
        (scope, _meaningful_persona(text))
        for scope, text in _persistent_state(ctx).persona_layers()
    ]
    visible = [(scope, text) for scope, text in layers if text]
    if not visible:
        return ("当前会话未设置有效人格。", [], None)
    merged = "\n\n".join(f"## {scope} 层\n{text}" for scope, text in visible)
    return (f"当前生效人格（后层优先）：\n----\n{merged}", [], None)


def _handler_persona_set(
    args: Dict[str, Any], ctx: ToolContext | None = None
) -> HandlerResult:
    _require_owner(ctx)
    text = require_arg(args, "text")
    scope = _resolve_scope(args, ctx)
    _persistent_state(ctx).persona_set(scope, text)
    return (f"已覆盖 {scope} 层人格。", [], None)


def _handler_persona_append(
    args: Dict[str, Any], ctx: ToolContext | None = None
) -> HandlerResult:
    _require_owner(ctx)
    text = require_arg(args, "text")
    scope = _resolve_scope(args, ctx)
    _persistent_state(ctx).persona_append(scope, text)
    return (f"已追加到 {scope} 层人格。", [], None)


def _handler_persona_clear(
    args: Dict[str, Any], ctx: ToolContext | None = None
) -> HandlerResult:
    _require_owner(ctx)
    if not bool(args.get("confirm", False)):
        raise ValueError("拒绝清空：persona_clear 需要 confirm=true 才会执行。")
    scope = _resolve_scope(args, ctx)
    _persistent_state(ctx).persona_clear(scope)
    return (f"{scope} 层人格已清空。", [], None)


_SCOPE_PROPERTY = {
    "type": "string",
    "description": (
        "目标层级：'global'=所有会话基础人格；'group'=仅当前群；"
        "'user'=仅当前私聊对象。未指定时群聊默认 group，私聊默认 user。"
    ),
    "enum": list(PERSONA_SCOPES),
}


TOOLS: List[ToolDef] = [
    ToolDef(
        name="persona_show",
        summary="Owner 查看当前会话生效的 global→group 或 global→user 人格。",
        properties={},
        required=[],
        handler=_handler_persona_show,
        aliases=["查看人格", "show_persona"],
        requires_role="owner",
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="persona_set",
        summary=(
            "Owner 覆盖一层机器人对话人格。Owner 指定模仿、直接作为或第一人称扮演"
            "某个人物/角色时保留原始要求，不自动弱化为相近原创风格。人格文本不能"
            "改变调用者角色、准入、工具授权、凭据边界或执行事实。"
        ),
        properties={
            "text": {"type": "string", "description": "完整人格设定文本。"},
            "scope": _SCOPE_PROPERTY,
        },
        required=["text"],
        handler=_handler_persona_set,
        aliases=["设定人格", "set_persona"],
        requires_role="owner",
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="persona_append",
        summary="Owner 向一层机器人对话人格追加补充设定，不覆盖已有内容。",
        properties={
            "text": {"type": "string", "description": "要追加的人格补充设定。"},
            "scope": _SCOPE_PROPERTY,
        },
        required=["text"],
        handler=_handler_persona_append,
        aliases=["追加人格", "append_persona"],
        requires_role="owner",
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="persona_clear",
        summary="Owner 清空一层人格；破坏性操作，必须显式设置 confirm=true。",
        properties={
            "scope": _SCOPE_PROPERTY,
            "confirm": {
                "type": "boolean",
                "description": "必须显式设为 true 才会执行。",
                "default": False,
            },
        },
        required=["confirm"],
        handler=_handler_persona_clear,
        aliases=["清空人格", "reset_persona"],
        requires_role="owner",
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
]


__all__ = ["TOOLS"]
