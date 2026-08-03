"""Session prompt assembly for ACP/application composition.

Platform adapters expose channel capabilities; this module owns how runtime
session context, role, bot prompts, skills, and platform facts become the
Agent system baseline.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from chatcopilot.agent.context.builtin_prompts import (
    default_memory,
    default_role,
    default_safety,
)
from chatcopilot.contracts.skills import SkillIndexEntry, render_skill_index_section
from chatcopilot.contracts.identity import AssistantMode, Role, role_value
from chatcopilot.middleware.runtime.workspace import Workspace


def build_system_prompt(
    *,
    platform_type: str,
    workspace: Workspace,
    role: Any = None,
    assistant_mode: Any = None,
    bot_system_prompt: str | None = None,
    bot_refusal_prompt: str | None = None,
    capability_prompt_fragments: tuple[str, ...] = (),
    skill_index: Sequence[SkillIndexEntry] = (),
    mode_prompts: Mapping[str, str] | None = None,
    role_prompts: Mapping[str, str] | None = None,
    safety_prompt: str | None = None,
    memory_prompt: str | None = None,
    llm_model: str | None = None,
) -> str:
    platform = (platform_type or "feishu").strip().lower()
    if platform == "qq":
        return _build_qq_prompt(
            workspace,
            role=role,
            bot_system_prompt=bot_system_prompt,
            bot_refusal_prompt=bot_refusal_prompt,
            capability_prompt_fragments=capability_prompt_fragments,
            skill_index=skill_index,
            role_prompts=role_prompts,
            safety_prompt=safety_prompt,
            llm_model=llm_model,
        )
    return _build_feishu_prompt(
        workspace,
        role=role or Role.USER,
        assistant_mode=assistant_mode or AssistantMode.PERFORMANCE,
        bot_system_prompt=bot_system_prompt,
        bot_refusal_prompt=bot_refusal_prompt,
        capability_prompt_fragments=capability_prompt_fragments,
        skill_index=skill_index,
        mode_prompts=mode_prompts,
        role_prompts=role_prompts,
        safety_prompt=safety_prompt,
        memory_prompt=memory_prompt,
        llm_model=llm_model,
    )


def _role_key(role: Any) -> str:
    value = role_value(role)
    if value == Role.OWNER.value:
        return "owner"
    if value == Role.ADMIN.value:
        return "admin"
    return "user"


def _mode_key(mode: Any) -> str:
    value = role_value(mode)
    if value == AssistantMode.GENERAL.value:
        return "general"
    return "performance"


def _is_qq_owner_role(role: Any) -> bool:
    if isinstance(role, Role):
        return role is Role.OWNER
    value = getattr(role, "value", role)
    return str(value) == Role.OWNER.value


def _format_role_patch(role: Any, role_prompts: Mapping[str, str] | None) -> str:
    key = _role_key(role)
    if role_prompts and key in role_prompts:
        return role_prompts[key].strip()
    return default_role(key)


def _format_mode_rules(mode: Any, mode_prompts: Mapping[str, str] | None) -> str:
    if not mode_prompts:
        return ""
    return mode_prompts.get(_mode_key(mode), "").strip()


def _format_capability_prompt_fragments(fragments: tuple[str, ...]) -> str:
    lines = [str(fragment).strip() for fragment in fragments if str(fragment).strip()]
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return "## 当前可用能力（BotSpec 注入）\n\n" + body


def _format_feishu_user_identity(ws: Workspace, role: Any, mode: Any) -> str:
    display = ws.user_name or "（未知，飞书未提供显示名）"
    role_label = {
        Role.OWNER.value: "**Owner**（主人 / 可选择通用模式；调试输出可切换）",
        Role.ADMIN.value: "**Admin**（管理员；固定性能分析模式）",
        Role.USER.value: "**User**（普通用户）",
    }.get(_role_key(role), "**User**（普通用户）")
    mode_label = {
        AssistantMode.PERFORMANCE.value: "**性能分析模式**（默认）",
        AssistantMode.GENERAL.value: "**通用模式**（仅 Owner 可选）",
    }.get(role_value(mode), "**性能分析模式**（默认）")
    return "\n".join([
        "## 当前用户身份（运行时注入）",
        "",
        f"- 显示名：{display}",
        f"- 角色：{role_label}",
        f"- 当前业务模式：{mode_label}",
        "",
    ])


def _workspace_counts(ws: Workspace) -> tuple[int, int, int]:
    counts: list[int] = []
    for path in (ws.attachments, ws.downloads, ws.results):
        try:
            counts.append(sum(1 for _ in path.iterdir()) if path.is_dir() else 0)
        except OSError:
            counts.append(0)
    return counts[0], counts[1], counts[2]


def _format_feishu_workspace_context(ws: Workspace, role: Any, llm_model: str | None = None) -> str:
    parts: list[str] = []
    is_owner = _role_key(role) == "owner"
    if ws.chat_kind == "p2p" and ws.user_id:
        if is_owner and ws.user_name:
            parts.append(f"- 这是一次**私聊**会话；当前用户已识别为：{ws.user_name}。")
        else:
            parts.append(f"- 这是一次**私聊**会话；当前用户内部标识：`{ws.user_id}`。")
    elif ws.chat_kind == "group" and ws.user_id:
        display = ws.user_name if is_owner and ws.user_name else f"`{ws.user_id}`"
        parts.append(
            f"- 这是一次**群聊**会话；当前发言人已识别为：{display}，群标识：`{ws.chat_id}`。"
            "**只对当前发言人服务**，不要把同群其他人的文件混进来。"
        )
    elif ws.user_id:
        display = ws.user_name if is_owner and ws.user_name else f"`{ws.user_id}`"
        parts.append(f"- 当前用户已识别为：{display}。")
    else:
        parts.append(
            "- 当前会话**无法识别用户身份**（部署 / 调试态），所有产物落到共享空间。"
            "**不要**告诉用户'你没有私人空间'——只是这次没有用户上下文，向用户解释为'本次会话未绑定身份'即可。"
        )

    att_count, down_count, results_count = _workspace_counts(ws)
    parts.append(f"- 该用户私人空间已建立：附件 {att_count} 项 / 历史下载 {down_count} 项 / 历史产物 {results_count} 项。")
    model = (llm_model or "").strip()
    if model:
        parts.append(f"- 当前 LLM 模型：`{model}`。当用户询问模型/API 时，可以直接引用该值。")
    if ws.memory_file.is_file() and ws.memory_file.stat().st_size > 64:
        parts.append("- 该用户**已有记事本**（长期偏好记录）。会话开局应主动调一次 `read_memory` 读它。")
    else:
        parts.append("- 该用户**还没有记事本内容**（首次见面或已被清空）。不要主动写入，只在用户告知可复用偏好时才考虑写。")
    return "## 当前会话上下文（运行时注入）\n\n" + "\n".join(parts) + "\n"


def _build_feishu_prompt(
    workspace: Workspace,
    *,
    role: Any,
    assistant_mode: Any,
    bot_system_prompt: str | None,
    bot_refusal_prompt: str | None,
    capability_prompt_fragments: tuple[str, ...],
    skill_index: Sequence[SkillIndexEntry],
    mode_prompts: Mapping[str, str] | None,
    role_prompts: Mapping[str, str] | None,
    safety_prompt: str | None,
    memory_prompt: str | None,
    llm_model: str | None,
) -> str:
    bot_parts = [bot_system_prompt or ""]
    if bot_refusal_prompt:
        bot_parts.append(bot_refusal_prompt)
    bot_prompt = "\n\n".join(part.strip() for part in bot_parts if part.strip())
    parts = [
        _format_feishu_workspace_context(workspace, role, llm_model),
        _format_feishu_user_identity(workspace, role, assistant_mode),
    ]
    if bot_prompt:
        parts.append(bot_prompt)
    capability_prompt = _format_capability_prompt_fragments(capability_prompt_fragments)
    if capability_prompt:
        parts.append(capability_prompt)
    skill_section = render_skill_index_section(skill_index)
    if skill_section:
        parts.append(skill_section)
    mode_rules = _format_mode_rules(assistant_mode, mode_prompts)
    if mode_rules:
        parts.append(mode_rules)
    parts.extend([
        (memory_prompt or "").strip() or default_memory(),
        (safety_prompt or "").strip() or default_safety(),
        _format_role_patch(role, role_prompts),
    ])
    return "\n".join(part for part in parts if part)


def _format_qq_session_header(llm_model: str | None = None) -> str:
    lines = [
        "## 当前会话上下文（运行时注入）",
        "",
        "- 这是一次 QQ 会话；由 cc-connect OneBot 通道维护当前用户身份。",
    ]
    model = (llm_model or "").strip()
    if model:
        lines.append(f"- 当前 LLM 模型：`{model}`。当用户询问模型/API 时，可以直接引用该值。")
    return "\n".join(lines) + "\n"


def _build_qq_prompt(
    workspace: Workspace,
    *,
    role: Any,
    bot_system_prompt: str | None,
    bot_refusal_prompt: str | None,
    capability_prompt_fragments: tuple[str, ...],
    skill_index: Sequence[SkillIndexEntry],
    role_prompts: Mapping[str, str] | None,
    safety_prompt: str | None,
    llm_model: str | None,
) -> str:
    del workspace
    owner = _is_qq_owner_role(role)
    parts: list[str] = [_format_qq_session_header(llm_model)]
    if bot_system_prompt:
        parts.append(bot_system_prompt.strip())
    capability_section = _format_capability_prompt_fragments(capability_prompt_fragments)
    if capability_section:
        parts.append(capability_section)
    skill_section = render_skill_index_section(skill_index)
    if skill_section:
        parts.append(skill_section)
    if owner:
        owner_prompt = (role_prompts or {}).get("owner", "")
        if owner_prompt:
            parts.append(owner_prompt.strip())
    else:
        if bot_refusal_prompt:
            parts.append(bot_refusal_prompt.strip())
        parts.append((safety_prompt or "").strip() or default_safety())
    return "\n\n".join(part for part in parts if part)


__all__ = ["build_system_prompt"]
