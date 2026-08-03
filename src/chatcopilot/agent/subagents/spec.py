"""Compatibility re-export for subagent contracts.

Canonical definitions live in :mod:`chatcopilot.contracts.subagents` so BotSpec
and Agent can share DTOs without BotSpec importing Agent internals.
"""
from __future__ import annotations

from chatcopilot.contracts.subagents import (
    CachePolicySpec,
    ContextPolicySpec,
    PromptLayerSpec,
    SubagentDef,
    SUBAGENT_RESULT_FIELDS,
    TASK_PACK_FIELDS,
    ToolMatchRule,
    ToolSelectorSpec,
    WorkflowDef,
)

__all__ = [
    "CachePolicySpec",
    "ContextPolicySpec",
    "PromptLayerSpec",
    "SubagentDef",
    "SUBAGENT_RESULT_FIELDS",
    "TASK_PACK_FIELDS",
    "ToolMatchRule",
    "ToolSelectorSpec",
    "WorkflowDef",
]
