"""Build short, explicit context packs for subagent calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from chatcopilot.agent.context.token_estimator import estimate_tokens
from chatcopilot.agent.subagents.spec import ContextPolicySpec
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.external_tools.shared.tool_spec import ToolDef


@dataclass(frozen=True)
class ContextPack:
    task: TaskPack
    allowed_tools: tuple[str, ...]
    tool_summaries: tuple[str, ...]
    policy: ContextPolicySpec
    memory_summary: str = ""
    rag_snippets: tuple[str, ...] = ()

    def render(self) -> str:
        payload: dict[str, Any] = {
            "task_pack": self.task.to_dict(),
            "context_policy": {
                "include_history": self.policy.include_history,
                "include_allowed_tools": self.policy.include_allowed_tools,
                "max_context_tokens": self.policy.max_context_tokens,
            },
        }
        if self.policy.include_allowed_tools:
            payload["allowed_tools"] = list(self.allowed_tools)
        if self.policy.include_tool_summary:
            payload["tool_summaries"] = list(self.tool_summaries)
        if self.memory_summary:
            payload["memory_summary"] = self.memory_summary
        if self.rag_snippets:
            payload["rag_snippets"] = list(self.rag_snippets)
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


class ContextPackBuilder:
    def build(
        self,
        *,
        task: TaskPack,
        tools: Sequence[ToolDef],
        policy: ContextPolicySpec,
        memory_summary: str | None = None,
        rag_snippets: Sequence[str] | None = None,
    ) -> ContextPack:
        allowed_fields = set(policy.allowed_task_fields)
        task_data = {
            key: value
            for key, value in task.to_dict().items()
            if key in allowed_fields and not _is_empty(value)
        }
        trimmed_task = TaskPack(
            objective=str(task_data.get("objective") or task.objective),
            user_intent=str(task_data.get("user_intent") or ""),
            deliverable=str(task_data.get("deliverable") or ""),
            constraints=tuple(task_data.get("constraints") or ()),
            inputs=tuple(task_data.get("inputs") or ()),
            resources=tuple(task_data.get("resources") or ()),
            acceptance_criteria=tuple(task_data.get("acceptance_criteria") or ()),
            evidence_required=tuple(task_data.get("evidence_required") or ()),
            domain=str(task_data.get("domain") or ""),
            target_sites=tuple(task_data.get("target_sites") or ()),
            time_window=str(task_data.get("time_window") or ""),
            required_fields=tuple(task_data.get("required_fields") or ()),
            cross_check=bool(task_data.get("cross_check", False)),
            write_scope=str(task_data.get("write_scope") or ""),
            excluded_context=tuple(task_data.get("excluded_context") or ()),
            cache_key_hint=str(task_data.get("cache_key_hint") or ""),
            legacy_task=task.legacy_task,
        )
        summaries = tuple(_tool_summary(tool) for tool in tools)
        trimmed_summaries = _trim_summaries(summaries, policy.max_context_tokens)

        mem = (memory_summary or "").strip()
        rags = tuple(s.strip() for s in (rag_snippets or ()) if s.strip())

        budget_left = policy.max_context_tokens - sum(estimate_tokens(s) for s in trimmed_summaries)
        if mem and estimate_tokens(mem) > budget_left:
            mem = ""
        budget_left -= estimate_tokens(mem)
        trimmed_rags: list[str] = []
        for snippet in rags:
            cost = estimate_tokens(snippet)
            if trimmed_rags and cost > budget_left:
                break
            trimmed_rags.append(snippet)
            budget_left -= cost

        return ContextPack(
            task=trimmed_task,
            allowed_tools=tuple(tool.name for tool in tools),
            tool_summaries=trimmed_summaries,
            policy=policy,
            memory_summary=mem,
            rag_snippets=tuple(trimmed_rags),
        )


def _tool_summary(tool: ToolDef) -> str:
    category = f" category={tool.category}" if tool.category else ""
    owner = f" owner={tool.owner}" if tool.owner else ""
    return f"{tool.name}:{category}{owner} {tool.summary}".strip()


def _trim_summaries(values: tuple[str, ...], max_tokens: int) -> tuple[str, ...]:
    out: list[str] = []
    budget = max(200, max_tokens // 4)
    used = 0
    for item in values:
        cost = estimate_tokens(item)
        if out and used + cost > budget:
            break
        out.append(item)
        used += cost
    return tuple(out)


def _is_empty(value: Any) -> bool:
    return value in (None, "", (), [])


__all__ = ["ContextPack", "ContextPackBuilder"]
