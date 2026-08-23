"""Compatibility ToolDef for the legacy research_information entrypoint."""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from chatcopilot.core.llm_client import LLMClient
from chatcopilot.agent.search.coordinator import SearchCoordinator as ResearchRunner
from chatcopilot.agent.search.providers import SearchProviderRegistry as ResearchSourceRegistry
from chatcopilot.agent.search.tool import build_search_tool
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.contracts.subagents import SubagentBudgetSpec
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult


def build_research_tool(
    *,
    main_llm: LLMClient,
    budget: SubagentBudgetSpec,
    tools: Sequence[ToolDef],
    raw_mcp_tools: Sequence[ToolDef] = (),
    turn_timeout_seconds: float | None = None,
    circuit: SearchCircuitBreaker | None = None,
) -> ToolDef | None:
    search_tool = build_search_tool(
        main_llm=main_llm,
        budget=budget,
        tools=tools,
        raw_mcp_tools=raw_mcp_tools,
        turn_timeout_seconds=turn_timeout_seconds,
        circuit=circuit,
    )
    if search_tool is None:
        return None

    def _handler(args: dict, ctx: ToolContext) -> ToolResult:
        result = search_tool.handler(args, ctx)
        if not result.ok and result.error_code == "invalid_search_request":
            return replace(result, error_code="invalid_research_request")
        return result

    return ToolDef(
        name="research_information",
        summary=(
            "Legacy alias for search_information. It plans source selection, "
            "searches approved web or vertical sources, reads concrete URLs, "
            "performs cross-checks when required, and returns structured evidence."
        ),
        input_schema=search_tool.input_schema,
        output_schema=search_tool.output_schema,
        handler=_handler,
        category="agent.research",
        owner=search_tool.owner,
        module=__name__,
        aliases=search_tool.aliases,
        doc_anchors=search_tool.doc_anchors,
        requires_role=search_tool.requires_role,
        artifact_kinds=search_tool.artifact_kinds,
        weight=search_tool.weight,
        execution_policy=search_tool.execution_policy,
        deprecated=True,
        metadata={
            **search_tool.metadata,
            "research_entry": True,
            "compat_entry": "search_information",
        },
    )

__all__ = ["ResearchRunner", "ResearchSourceRegistry", "build_research_tool"]
