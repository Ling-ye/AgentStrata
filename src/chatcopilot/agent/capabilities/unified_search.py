"""Session contributor for the unified search entrypoint."""
from __future__ import annotations

from chatcopilot.agent.capabilities.assembly import SessionCapabilityContext
from chatcopilot.agent.search.tool import build_search_provider
from chatcopilot.contracts.tool_packs import ToolProvider


def build_provider(
    context: SessionCapabilityContext,
) -> ToolProvider | None:
    direct_codex = context.backend_id == "codex"
    if not context.subagents.research_enabled or (
        direct_codex and not context.subagents.codex.allow_unified_search_tool
    ):
        return None
    accessible_base_tools = tuple(
        tool
        for tool in context.base_tools
        if context.permission_filter is None
        or context.permission_filter(tool) is None
    )
    accessible_contributed_tools = tuple(
        tool
        for tool in context.contributed_tools
        if context.permission_filter is None
        or context.permission_filter(tool) is None
    )
    raw_mcp_search_tools = tuple(
        tool
        for tool in (context.subagent_tools or context.base_tools)
        if tool.category == "mcp"
        and str(tool.metadata.get("mcp_risk", "")) == "search"
    )
    return build_search_provider(
        main_llm=context.research_llm,
        budget=context.subagents.research_budget,
        tools=(*accessible_base_tools, *accessible_contributed_tools),
        raw_mcp_tools=raw_mcp_search_tools,
        provider_specs=context.subagents.search_providers,
        turn_timeout_seconds=context.runtime_config.runtime.turn_timeout_seconds,
        circuit=context.search_circuit,
    )


__all__ = ["build_provider"]
