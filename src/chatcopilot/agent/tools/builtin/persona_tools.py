"""个性（persona）工具：persona_show / persona_set / persona_append / persona_clear。

分层落盘到当前会话的 ``PERSONA.md``（全局 / 群 / 个人三层），由
``MarkdownPersonaProvider`` + ``agent.persona.layers`` 负责定位与合并。读操作
对所有人开放；写操作对所有白名单用户开放（user/group scope），global scope
仅 owner 可写——通过 ``_require_scope_permission`` 在 handler 内检查
``get_caller_role_hint()`` 实现。
"""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.agent.persona.layers import (
    PERSONA_SCOPES,
    merge_persona_layers,
    persona_layer_specs,
    persona_path_for_scope,
)
from chatcopilot.agent.persona.markdown import MarkdownPersonaProvider
from chatcopilot.agent.tools.workspace_context import (
    describe_workspace,
    resolve_workspace,
    resolve_workspace_root,
)
from chatcopilot.core.caller_context import get_caller_role_hint
from chatcopilot.external_tools.shared.spec_helpers import require_arg
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef


def _layer_specs():
    ws = resolve_workspace(create=True)
    workspace_root = resolve_workspace_root(ws)
    specs = persona_layer_specs(
        workspace_root=workspace_root,
        user_root=ws.root,
        chat_kind=ws.chat_kind,
        chat_id=ws.chat_id,
    )
    return ws, workspace_root, specs


def _target_path(scope: str):
    ws = resolve_workspace(create=True)
    workspace_root = resolve_workspace_root(ws)
    path = persona_path_for_scope(
        scope,
        workspace_root=workspace_root,
        user_root=ws.root,
        chat_kind=ws.chat_kind,
        chat_id=ws.chat_id,
    )
    return ws, path


def _default_scope_for_workspace() -> str:
    ws = resolve_workspace(create=True)
    return "group" if (ws.chat_kind or "").strip().lower() == "group" and ws.chat_id else "user"


def _resolve_scope(args: Dict[str, Any]) -> str:
    raw = args.get("scope")
    if raw is None or not str(raw).strip():
        return _default_scope_for_workspace()
    return str(raw).strip().lower()


def _require_scope_permission(scope: str) -> None:
    """Raise if the caller lacks write permission for the given scope.

    global scope requires owner; group / user open to all whitelisted users.
    """
    if scope == "global" and get_caller_role_hint() != "owner":
        raise PermissionError(
            "全局（global）层个性仅 owner 可修改；"
            "你可以使用 scope=group（群级）或 scope=user（个人级）设定个性。"
        )


def _handler_persona_show(args: Dict[str, Any]) -> HandlerResult:
    ws, _, specs = _layer_specs()
    merged = merge_persona_layers(specs)
    outputs = [str(path) for _, path in specs]
    if not merged:
        return (
            f"{describe_workspace(ws)}\n当前未设置任何个性（全局/群/个人三层均为空）。"
            "白名单用户均可通过 persona_set 设定个性（global 层仅 owner 可改）。",
            outputs,
            None,
        )
    return (f"{describe_workspace(ws)}\n当前生效个性（全局→群→个人合并）：\n----\n{merged}", outputs, None)


def _handler_persona_set(args: Dict[str, Any]) -> HandlerResult:
    text = require_arg(args, "text")
    scope = _resolve_scope(args)
    _require_scope_permission(scope)
    ws, path = _target_path(scope)
    MarkdownPersonaProvider(path).set(text)
    return (f"已覆盖 {scope} 层个性：{ws.relpath(path)}", [str(path)], None)


def _handler_persona_append(args: Dict[str, Any]) -> HandlerResult:
    text = require_arg(args, "text")
    scope = _resolve_scope(args)
    _require_scope_permission(scope)
    ws, path = _target_path(scope)
    MarkdownPersonaProvider(path).append(text)
    return (f"已追加到 {scope} 层个性：{ws.relpath(path)}", [str(path)], None)


def _handler_persona_clear(args: Dict[str, Any]) -> HandlerResult:
    confirm = bool(args.get("confirm", False))
    if not confirm:
        raise ValueError("拒绝清空：persona_clear 需要 confirm=true 才会执行。")
    scope = _resolve_scope(args)
    _require_scope_permission(scope)
    ws, path = _target_path(scope)
    MarkdownPersonaProvider(path).clear()
    return (f"{scope} 层个性已重置：{ws.relpath(path)}", [str(path)], None)


_SCOPE_PROPERTY = {
    "type": "string",
    "description": (
        "目标层级："
        "'global'=对所有人生效的基础人格；"
        "'group'=仅当前群聊（私聊不可用）；"
        "'user'=仅当前对象专属。未指定时：群聊默认 group，私聊默认 user。"
    ),
    "enum": list(PERSONA_SCOPES),
}


TOOLS: List[ToolDef] = [
    ToolDef(
        name="persona_show",
        summary=(
            "查看当前对当前对象生效的个性设定（全局 → 群 → 个人三层合并后的人格/语气/风格）。"
            "任何人都能查看；个性会自动注入到你的 system prompt，通常无需手动调用。"
        ),
        properties={},
        required=[],
        handler=_handler_persona_show,
        aliases=["查看个性", "show_persona"],
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="persona_set",
        summary=(
            "覆盖式设定某一层个性（人格/语气/称呼/立场）。"
            "白名单用户均可设定 group/user 层；global 层仅 owner 可用。"
            "当用户说'以后对我毒舌一点'、'在这个群里正式一些'、"
            "'你的基础人格设为……'时使用，把完整人格描述写进对应 scope 层。"
        ),
        properties={
            "text": {
                "type": "string",
                "description": "完整的人格设定文本（会覆盖该层原有内容）。",
            },
            "scope": _SCOPE_PROPERTY,
        },
        required=["text"],
        handler=_handler_persona_set,
        aliases=["设定个性", "set_persona"],
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="persona_append",
        summary=(
            "在某一层个性末尾追加一条补充设定（不覆盖原有）。"
            "白名单用户均可追加 group/user 层；global 层仅 owner 可用。"
            "适合在已有个性上微调，如新增一条口头禅或偏好。"
        ),
        properties={
            "text": {
                "type": "string",
                "description": "要追加的人格补充设定。",
            },
            "scope": _SCOPE_PROPERTY,
        },
        required=["text"],
        handler=_handler_persona_append,
        aliases=["追加个性", "append_persona"],
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="persona_clear",
        summary=(
            "清空某一层个性，重置为初始模板。**破坏性操作**，"
            "必须把 confirm 显式设为 true。"
            "白名单用户可清空 group/user 层；global 层仅 owner 可用。"
        ),
        properties={
            "scope": _SCOPE_PROPERTY,
            "confirm": {
                "type": "boolean",
                "description": "必须显式设为 true 才会执行；false / 缺失则拒绝。",
                "default": False,
            },
        },
        required=["confirm"],
        handler=_handler_persona_clear,
        aliases=["清空个性", "reset_persona"],
        category="agent.persona",
        owner="agent",
        module=__name__,
    ),
]


__all__ = ["TOOLS"]
