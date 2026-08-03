"""Compile a :class:`ToolSelectorSpec` into a tool predicate.

This is the single place that turns declarative selection data into a runnable
``ToolDef -> bool`` predicate, and the single place that enforces the
*user-facing ban*: a subagent is an internal worker for the main agent and may
never touch tools that talk to the end user directly (e.g. ``send_files_to_user``).
Such tools are marked ``metadata['user_facing'] = True``.
"""

from __future__ import annotations

from typing import Callable, Sequence

from chatcopilot.agent.subagents.spec import ToolMatchRule, ToolSelectorSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef

ToolPredicate = Callable[[ToolDef], bool]


def is_user_facing(tool: ToolDef) -> bool:
    """Tools that deliver output straight to the user; never given to a subagent."""
    return bool(tool.metadata.get("user_facing"))


def _tool_tags(tool: ToolDef) -> tuple[str, ...]:
    raw = tool.metadata.get("tags") or ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw)
    return ()


def _rule_matches(rule: ToolMatchRule, tool: ToolDef) -> bool:
    if rule.is_empty:
        return False
    if rule.names and tool.name not in rule.names:
        return False
    if rule.name_prefixes and not any(tool.name.startswith(p) for p in rule.name_prefixes):
        return False
    if rule.categories and tool.category not in rule.categories:
        return False
    if rule.category_prefixes and not any(
        tool.category.startswith(p) for p in rule.category_prefixes
    ):
        return False
    if rule.owners and tool.owner.lower() not in {o.lower() for o in rule.owners}:
        return False
    if rule.module_prefixes and not any(tool.module.startswith(p) for p in rule.module_prefixes):
        return False
    if rule.tags:
        tags = {t.lower() for t in _tool_tags(tool)}
        if not any(t.lower() in tags for t in rule.tags):
            return False
    if rule.mcp_risk:
        risk = str(tool.metadata.get("mcp_risk", "")).lower()
        if risk not in {r.lower() for r in rule.mcp_risk}:
            return False
    return True


def build_predicate(selector: ToolSelectorSpec) -> ToolPredicate:
    """Compile ``selector`` into a predicate; always excludes user-facing tools."""

    def _predicate(tool: ToolDef) -> bool:
        if is_user_facing(tool):
            return False
        if tool.name in selector.exclude_names:
            return False
        return any(_rule_matches(rule, tool) for rule in selector.any)

    return _predicate


__all__ = ["ToolPredicate", "build_predicate", "is_user_facing"]
