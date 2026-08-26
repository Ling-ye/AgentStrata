"""Runtime provider factory for the Bot-local playbook reader."""
from __future__ import annotations

from chatcopilot.agent.capabilities.assembly import RuntimeCapabilityContext
from chatcopilot.agent.tools.builtin.skill_tools import build_skill_provider
from chatcopilot.contracts.tool_packs import ToolProvider


def build_provider(context: RuntimeCapabilityContext) -> ToolProvider:
    return build_skill_provider(context.skill_index)


__all__ = ["build_provider"]
