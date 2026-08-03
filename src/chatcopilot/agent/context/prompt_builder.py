"""装配 system prompt。

agent 内部的 system prompt = ``baseline``（上层提供的角色无关基线，如机器人人格 +
能力声明 + 安全规则）+ 可选的 skills 索引片段。memory 摘要由 agent 在
``new_session`` 时通过 MemoryProvider 主动注入到 baseline 之后；本模块只负责拼接。

Prompt prefix-cache 优化：越稳定的内容越靠前，per-session/per-day 变化的内容
放在最后——DeepSeek 等 provider 按 token-0 开始的精确前缀匹配，前缀越长命中越多。
"""
from __future__ import annotations

from datetime import date
from typing import Sequence

from chatcopilot.agent.context.builtin_prompts import default_accuracy, default_search_first
from chatcopilot.agent.search_policy import render_search_routing_policy
from chatcopilot.contracts.skills import SkillIndexEntry, render_skill_index_section


def build_system_prompt(
    *,
    baseline: str,
    skill_index: Sequence[SkillIndexEntry] = (),
    memory_snippet: str | None = None,
    has_search_tools: bool = False,
    search_tool_names: Sequence[str] = (),
    session_dynamic_tail: str | None = None,
) -> str:
    """拼接最终发给 LLM 的 system prompt。

    顺序（按稳定性从高到低排列，最大化 prefix cache 命中）：
    1. baseline —— 上层装配好的角色/能力/安全片段（跨 session 稳定）
    2. accuracy —— 框架级准确性与抗迎合规则（跨 bot 稳定）
    3. search_first + routing policy —— 搜索相关（跨 session 稳定）
    4. skill 索引 —— 可选（跨 session 稳定）
    --- 以下为 per-session 或 per-day 变化的动态内容 ---
    5. session_dynamic_tail —— persona overlay 等 per-session 动态内容
    6. memory_snippet —— 长期记忆摘要（per-session 可变）
    7. 当前日期 —— 放最末（每日变一次）
    """
    parts: list[str] = []
    baseline_text = (baseline or "").strip()
    if baseline_text:
        parts.append(baseline_text)
    parts.append(default_accuracy())
    if has_search_tools:
        parts.append(default_search_first())
        routing_policy = render_search_routing_policy(search_tool_names)
        if routing_policy:
            parts.append(routing_policy)
    skill_section = render_skill_index_section(skill_index)
    if skill_section:
        parts.append(skill_section.strip())
    # --- dynamic tail (per-session / per-day) ---
    dynamic_tail = (session_dynamic_tail or "").strip()
    if dynamic_tail:
        parts.append(dynamic_tail)
    snippet = (memory_snippet or "").strip()
    if snippet:
        parts.append(snippet)
    parts.append(f"## 当前日期\n\n今天是 {date.today().isoformat()}。")
    return "\n\n".join(parts)


__all__ = ["build_system_prompt"]
