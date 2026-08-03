"""ACP 元命令短路与会话级工具构造。

中间件在把 prompt 交给 agent 之前先做几道确定性短路，命中后**不进 agent**：
- ``/debug on|off|status`` 与同义自然语言：开关调试模式
- ``切换到通用模式`` / ``回到性能模式`` 等：切换业务模式
- Owner 查询运行时访问白名单：直接回包
- Owner 的"几个用户使用过/全局工作区状态"：直接回包

LLM 也可以通过 ``set_assistant_mode`` / ``set_debug_mode`` 两个工具间接触发同样
的状态变更（绕过元命令解析时），故工具与短路处理共享同一组校验/装配函数。
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, Mapping, Optional

from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef
from chatcopilot.middleware.access_control import (
    AssistantMode,
    Role,
    can_select_general_mode,
    can_toggle_debug,
    normalize_chat_kind,
)
from chatcopilot.middleware.acp.prompt_assembler import build_system_prompt
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.runtime.workspace import (
    Workspace,
    list_workspace_inventories,
    resolve_workspace_root,
)
# ----------------------------------------------------------------------------
# 常量：正则与提示文案
# ----------------------------------------------------------------------------
# 行为见 access_control.default_debug_mode + can_toggle_debug：
# - 群聊：任何角色都固定关闭 debug
# - 私聊：仅 OWNER 可通过 /debug 或自然语言命令临时开启/关闭
# 切换仅 per-session、不持久化（避免上次开了忘关给下次别人看到）。
# 命中调试模式命令时本轮不进 AgentSession.run_task，也不写 transcript（元操作不算业务对话）。
_DEBUG_CMD_RE = re.compile(r"^\s*/debug(?:\s+(\S+))?\s*$", re.IGNORECASE)
_DEBUG_ON_INTENT_RE = re.compile(r"(开启|打开|启用|切换到|进入).{0,12}(debug|调试模式)", re.IGNORECASE)
_DEBUG_OFF_INTENT_RE = re.compile(r"(关闭|关掉|退出|切回).{0,12}(debug|调试模式)", re.IGNORECASE)
_GENERAL_MODE_INTENT_RE = re.compile(r"(切换|进入|开启|启用|换到|改成|设为|设置为).{0,12}通用模式")
_PERFORMANCE_MODE_INTENT_RE = re.compile(
    r"(切换|进入|回到|返回|换到|改成|设为|设置为).{0,12}(性能分析模式|性能模式)"
)
_DEBUG_USAGE = (
    "用法：\n"
    "  /debug on        开启调试模式（思考过程一并推送）\n"
    "  /debug off       关闭调试模式（只推送最终结论）\n"
    "  /debug status    查看当前模式与切换权限"
)
_DEBUG_DENIED_GROUP = "群聊固定为性能分析模式，调试模式保持关闭。"
_DEBUG_DENIED_P2P = "调试模式仅限 Owner 私聊开启；当前会话固定为「只看最终结论」模式。"
_GENERAL_DENIED_GROUP = "群聊固定为性能分析模式，不能切换到通用模式。"
_GENERAL_DENIED_P2P = "通用模式仅限 Owner 私聊可用；当前用户只能使用性能分析模式。"

_OWNER_GLOBAL_WORKSPACE_INTENT_RE = re.compile(
    r"("
    r"几个用户|多少用户|用户数|使用人数|"
    r"哪些用户.{0,8}(使用|用过)|"
    r"用户.{0,8}(使用|用过|工作区|私人空间|存储|状态)|"
    r"其他用户|全局工作区|当前存储|存了哪些数据"
    r")"
)
_OWNER_RUNTIME_ACCESS_INTENT_RE = re.compile(
    r"("
    r"白名单|QQ_ALLOW_FROM|允许.{0,8}(谁|哪些|来源|用户|QQ|访问|使用)|"
    r"(谁|哪些|什么人).{0,8}(能|可以|允许).{0,8}(访问|使用|用)"
    r")",
    re.IGNORECASE,
)
_QQ_NUMBER_RE = re.compile(r"(?<!\d)([1-9]\d{4,11})(?!\d)")


# ----------------------------------------------------------------------------
# Mode / Debug 校验辅助
# ----------------------------------------------------------------------------
def _assistant_mode_label(mode: AssistantMode) -> str:
    if mode == AssistantMode.GENERAL:
        return "通用模式"
    return "性能分析模式"


def _is_group_session(session: SessionState) -> bool:
    return normalize_chat_kind(session.workspace.chat_kind, session.workspace.chat_id) == "group"


def _can_session_select_general_mode(session: SessionState) -> bool:
    return can_select_general_mode(
        session.role,
        session.workspace.chat_kind,
        session.workspace.chat_id,
    )


def _can_session_toggle_debug(session: SessionState) -> bool:
    return can_toggle_debug(
        session.role,
        session.workspace.chat_kind,
        session.workspace.chat_id,
    )


def _general_denied_message(session: SessionState) -> str:
    if _is_group_session(session):
        return _GENERAL_DENIED_GROUP
    return _GENERAL_DENIED_P2P


def _debug_denied_message(session: SessionState) -> str:
    if _is_group_session(session):
        return _DEBUG_DENIED_GROUP
    return _DEBUG_DENIED_P2P


def _debug_toggle_hint(session: SessionState) -> str:
    if _can_session_toggle_debug(session):
        return "可切换"
    if _is_group_session(session):
        return "不可切换（群聊固定关闭）"
    return "不可切换（仅限 Owner 私聊）"


def _force_performance_mode(session: SessionState) -> None:
    if session.assistant_mode == AssistantMode.PERFORMANCE:
        return
    system_prompt = build_system_prompt(
        platform_type=getattr(session.runtime, "platform_type", "feishu"),
        workspace=session.workspace,
        role=session.role,
        assistant_mode=AssistantMode.PERFORMANCE,
        bot_system_prompt=session.bot_system_prompt,
        bot_refusal_prompt=session.bot_refusal_prompt,
        capability_prompt_fragments=session.capability_prompt_fragments,
        skill_index=session.skill_index,
        mode_prompts=session.mode_prompt_overrides,
        role_prompts=session.role_prompt_overrides,
        safety_prompt=session.safety_prompt_override,
        memory_prompt=session.memory_prompt_override,
        llm_model=session.llm_model,
    )
    session.set_assistant_mode(AssistantMode.PERFORMANCE, system_prompt)


# ----------------------------------------------------------------------------
# LLM 工具：set_assistant_mode / set_debug_mode
# ----------------------------------------------------------------------------
def _build_set_assistant_mode_tool(session_getter: Callable[[], SessionState]) -> ToolDef:
    """构造飞书会话本地工具：由 LLM 调用来切换业务模式。"""

    def _handler(args: Dict[str, Any]) -> HandlerResult:
        session = session_getter()
        raw_mode = str(args.get("mode") or "").strip().lower()
        try:
            desired = AssistantMode(raw_mode)
        except ValueError as exc:
            raise ValueError("mode 只能是 performance 或 general") from exc

        if desired == AssistantMode.GENERAL and not _can_session_select_general_mode(session):
            _force_performance_mode(session)
            return (_general_denied_message(session), [], None)

        if session.assistant_mode == desired:
            return (
                f"当前已经是{_assistant_mode_label(desired)}，无需切换。",
                [],
                None,
            )

        system_prompt = build_system_prompt(
            platform_type=getattr(session.runtime, "platform_type", "feishu"),
            workspace=session.workspace,
            role=session.role,
            assistant_mode=desired,
            bot_system_prompt=session.bot_system_prompt,
            bot_refusal_prompt=session.bot_refusal_prompt,
            capability_prompt_fragments=session.capability_prompt_fragments,
            skill_index=session.skill_index,
            mode_prompts=session.mode_prompt_overrides,
            role_prompts=session.role_prompt_overrides,
            safety_prompt=session.safety_prompt_override,
            memory_prompt=session.memory_prompt_override,
            llm_model=session.llm_model,
        )
        session.set_assistant_mode(desired, system_prompt)
        return (
            f"已切换到{_assistant_mode_label(desired)}。",
            [],
            None,
        )

    return ToolDef(
        name="set_assistant_mode",
        summary=(
            "切换或查询当前飞书会话的业务模式。"
            "当用户要求进入通用模式、回到性能分析模式、询问当前业务模式，"
            "或用自然语言表达类似意图时调用本工具；不要自行声称已切换。"
            "通用模式仅 Owner 私聊可用，工具会做最终权限校验。"
        ),
        properties={
            "mode": {
                "type": "string",
                "enum": [AssistantMode.PERFORMANCE.value, AssistantMode.GENERAL.value],
                "description": "目标业务模式：performance=性能分析模式；general=通用模式。",
            },
            "reason": {
                "type": "string",
                "description": "用户请求切换模式的简短原因或原话摘要。",
            },
        },
        required=["mode"],
        handler=_handler,
        aliases=["业务模式切换", "切换通用模式", "切换性能分析模式"],
        weight="light",
    )


def _build_set_debug_mode_tool(session_getter: Callable[[], SessionState]) -> ToolDef:
    """构造飞书会话本地工具：由 LLM 调用来切换调试输出模式。"""

    def _handler(args: Dict[str, Any]) -> HandlerResult:
        session = session_getter()
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in {"on", "off", "status"}:
            raise ValueError("mode 只能是 on / off / status")

        if mode == "status":
            return (
                f"当前调试模式：{'on' if session.debug_mode else 'off'}；"
                f"当前角色：{session.role.value}（{_debug_toggle_hint(session)}）。",
                [],
                None,
            )

        if not _can_session_toggle_debug(session):
            session.debug_mode = False
            return (_debug_denied_message(session), [], None)

        desired = mode == "on"
        if session.debug_mode == desired:
            return (f"调试模式已经是 {mode}，无需切换。", [], None)

        session.debug_mode = desired
        if desired:
            return ("调试模式已开启：本会话内将一并推送 LLM 中间文本与工具调用进度。", [], None)
        return ("调试模式已关闭：本会话内只推送每轮的最终结论。", [], None)

    return ToolDef(
        name="set_debug_mode",
        summary=(
            "切换或查询当前飞书会话的调试输出模式。"
            "当用户用自然语言要求开启调试、关闭调试、只看最终结论、查看调试模式时调用本工具。"
            "不要自行声称已经切换；仅 Owner 私聊可切换，工具会做权限校验。"
        ),
        properties={
            "mode": {
                "type": "string",
                "enum": ["on", "off", "status"],
                "description": "目标调试模式：on=推送中间过程；off=只推最终结论；status=查询当前状态。",
            },
            "reason": {
                "type": "string",
                "description": "用户请求切换或查询调试模式的简短原因或原话摘要。",
            },
        },
        required=["mode"],
        handler=_handler,
        aliases=["调试模式切换", "开启调试模式", "关闭调试模式", "只看最终结论"],
        weight="light",
    )


# ----------------------------------------------------------------------------
# 元命令解析与短路 handler
# ----------------------------------------------------------------------------
def _parse_debug_command(text: str) -> Optional[str]:
    """解析明确的 debug 元命令。

    自然语言也在服务端先处理，避免 LLM 在工具拒绝后仍生成"已开启"的误导回复。
    """
    raw = (text or "").strip()
    match = _DEBUG_CMD_RE.match(raw)
    if match is not None:
        return (match.group(1) or "status").lower()
    if _DEBUG_ON_INTENT_RE.search(raw):
        return "on"
    if _DEBUG_OFF_INTENT_RE.search(raw):
        return "off"
    return None


def _parse_assistant_mode_command(text: str) -> Optional[AssistantMode]:
    """解析明确的业务模式切换元命令。"""
    raw = (text or "").strip()
    if _GENERAL_MODE_INTENT_RE.search(raw):
        return AssistantMode.GENERAL
    if _PERFORMANCE_MODE_INTENT_RE.search(raw):
        return AssistantMode.PERFORMANCE
    return None


def _handle_assistant_mode_command(session: SessionState, text: str) -> Optional[str]:
    desired = _parse_assistant_mode_command(text)
    if desired is None:
        return None

    if desired == AssistantMode.GENERAL and not _can_session_select_general_mode(session):
        _force_performance_mode(session)
        return _general_denied_message(session)

    if session.assistant_mode == desired:
        return f"当前已经是{_assistant_mode_label(desired)}，无需切换。"

    system_prompt = build_system_prompt(
        platform_type=getattr(session.runtime, "platform_type", "feishu"),
        workspace=session.workspace,
        role=session.role,
        assistant_mode=desired,
        bot_system_prompt=session.bot_system_prompt,
        bot_refusal_prompt=session.bot_refusal_prompt,
        capability_prompt_fragments=session.capability_prompt_fragments,
        skill_index=session.skill_index,
        mode_prompts=session.mode_prompt_overrides,
        role_prompts=session.role_prompt_overrides,
        safety_prompt=session.safety_prompt_override,
        memory_prompt=session.memory_prompt_override,
        llm_model=session.llm_model,
    )
    session.set_assistant_mode(desired, system_prompt)
    return f"已切换到{_assistant_mode_label(desired)}。"


def _handle_debug_command(session: SessionState, text: str) -> Optional[str]:
    """识别并处理调试模式元命令；返回需要回复给用户的文本，``None`` 表示
    不是 debug 命令（继续走正常 prompt 流程）。

    解析放在 ``extract_prompt_parts`` 之后、附件短路之前——避免用户附件 + 正文带
    其它内容时误触发。
    """
    sub = _parse_debug_command(text)
    if sub is None:
        return None

    role_label = session.role.value
    if sub == "status" or sub == "":
        return (
            f"当前调试模式：{'on' if session.debug_mode else 'off'}\n"
            f"当前角色：{role_label}（{_debug_toggle_hint(session)}）\n"
            f"{_DEBUG_USAGE}"
        )

    if sub not in ("on", "off"):
        return _DEBUG_USAGE

    if not _can_session_toggle_debug(session):
        session.debug_mode = False
        return _debug_denied_message(session)

    desired = sub == "on"
    if session.debug_mode == desired:
        return f"调试模式已经是 {sub}，无需切换。"
    session.debug_mode = desired
    if desired:
        return "✅ 调试模式已开启：本会话内将一并推送 LLM 中间文本与工具调用进度。"
    return "✅ 调试模式已关闭：本会话内只推送每轮的最终结论。"


# ----------------------------------------------------------------------------
# Owner 运行时信息短路
# ----------------------------------------------------------------------------
def _parse_runtime_allowlist(raw: str | None) -> tuple[list[str], bool]:
    """解析访问白名单 env；返回 ``(成员列表, 是否放行所有人)``。"""
    value = (raw or "").strip().strip('"').strip("'")
    if not value or value == "*":
        return [], True
    items: list[str] = []
    seen: set[str] = set()
    for token in value.split(","):
        item = token.strip().strip('"').strip("'").rstrip("\\")
        if not item:
            continue
        if item == "*":
            return [], True
        if item not in seen:
            seen.add(item)
            items.append(item)
    return items, not items


def _is_owner_runtime_info_query(user_text: str) -> bool:
    text = re.sub(r"\s+", "", user_text or "")
    return bool(text and _OWNER_RUNTIME_ACCESS_INTENT_RE.search(text))


def _format_owner_access_allowlist_status(
    session: SessionState,
    user_text: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    runtime = getattr(session, "runtime", None)
    access = getattr(runtime, "access", None)
    env_name = getattr(access, "whitelist_env", None) or ""
    if not env_name:
        return "当前 BotSpec 没有声明访问白名单 env，因此中间件不会按白名单限制来源。"

    resolved_env = env if env is not None else os.environ
    raw_value = resolved_env.get(env_name)
    allow_ids, allow_all = _parse_runtime_allowlist(raw_value)
    queried_ids = _QQ_NUMBER_RE.findall(user_text or "")

    header = f"当前访问白名单来源：`{env_name}`。"
    if allow_all:
        body = f"`{env_name}` 当前为空或 `*`，按访问门禁逻辑等同于允许所有来源。"
    else:
        body = f"当前共有 {len(allow_ids)} 个允许来源：{', '.join(allow_ids)}。"

    checks: list[str] = []
    if queried_ids:
        allow_set = set(allow_ids)
        for qq in queried_ids:
            if allow_all:
                checks.append(f"- `{qq}`：允许（当前白名单放行所有来源）")
            elif qq in allow_set:
                checks.append(f"- `{qq}`：在白名单中")
            else:
                checks.append(f"- `{qq}`：不在白名单中")

    if checks:
        return "\n".join([header, body, "查询结果：", *checks])
    return "\n".join([header, body])


def _handle_owner_runtime_info_query(
    session: SessionState,
    user_text: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Optional[str]:
    if getattr(session, "role", None) != Role.OWNER:
        return None
    if not _is_owner_runtime_info_query(user_text):
        return None
    return _format_owner_access_allowlist_status(session, user_text, env=env)


# ----------------------------------------------------------------------------
# Owner 全局工作区短路
# ----------------------------------------------------------------------------
def _format_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num} B"


def _is_owner_global_workspace_query(user_text: str) -> bool:
    text = re.sub(r"\s+", "", user_text or "")
    return bool(text and _OWNER_GLOBAL_WORKSPACE_INTENT_RE.search(text))


def _should_handle_owner_global_workspace_query(session: Any, user_text: str) -> bool:
    return getattr(session, "role", None) == Role.OWNER and _is_owner_global_workspace_query(user_text)


def _format_owner_global_workspace_status(current_workspace: Workspace) -> str:
    root = resolve_workspace_root(current_workspace)
    inventories = list_workspace_inventories(root)

    user_names: dict[str, set[str]] = {}
    user_workspace_counts: dict[str, int] = {}
    for item in inventories:
        if not item.user_id:
            continue
        user_workspace_counts[item.user_id] = user_workspace_counts.get(item.user_id, 0) + 1
        if item.user_name:
            user_names.setdefault(item.user_id, set()).add(item.user_name)

    total_files = sum(item.total_files for item in inventories)
    total_bytes = sum(item.total_bytes for item in inventories)
    named_user_count = sum(1 for user_id in user_workspace_counts if user_names.get(user_id))

    lines = [
        f"当前一共有 {len(user_workspace_counts)} 个明确用户使用过机器人。",
        (
            f"已识别工作区 {len(inventories)} 个；"
            f"含明确 user_id 的用户 {len(user_workspace_counts)} 个；"
            f"含飞书姓名的用户 {named_user_count} 个；"
            f"总文件 {total_files} 个；总大小 {_format_bytes(total_bytes)}。"
        ),
        f"workspace_root={root}",
    ]

    if user_workspace_counts:
        lines.append("用户明细：")
        for user_id in sorted(user_workspace_counts):
            names = "、".join(sorted(user_names.get(user_id, set()))) or "-"
            lines.append(f"- user_id={user_id} name={names} workspaces={user_workspace_counts[user_id]}")
    else:
        lines.append("没有识别到带明确 user_id 的工作区。")

    return "\n".join(lines)


__all__ = [
    "_build_set_assistant_mode_tool",
    "_build_set_debug_mode_tool",
    "_format_owner_access_allowlist_status",
    "_format_owner_global_workspace_status",
    "_handle_assistant_mode_command",
    "_handle_debug_command",
    "_handle_owner_runtime_info_query",
    "_is_owner_global_workspace_query",
    "_is_owner_runtime_info_query",
    "_parse_runtime_allowlist",
    "_parse_assistant_mode_command",
    "_parse_debug_command",
    "_should_handle_owner_global_workspace_query",
]
