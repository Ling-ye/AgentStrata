"""ToolDef entrypoint for the unified search coordinator."""

from __future__ import annotations

import json
from typing import Any, Sequence

from chatcopilot.agent.search.reranker import ResultReranker
from chatcopilot.agent.search.coordinator import SearchCoordinator
from chatcopilot.agent.search.models import SearchRequest
from chatcopilot.agent.search.page_reader import PageReader
from chatcopilot.agent.search.providers import (
    DirectSearchProvider,
    SearchProviderRegistry,
)
from chatcopilot.agent.search.router import SearchRouter
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.contracts.subagents import SearchProviderSpec, SubagentBudgetSpec
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef

_MAX_SEARCH_WALL_SECONDS = 180.0
_SEARCH_BUDGET_RATIO = 0.6
_MAX_PAGE_SUMMARY_CHARS = 12000


def build_search_tool(
    *,
    main_llm: LLMClient,
    budget: SubagentBudgetSpec,
    tools: Sequence[ToolDef],
    raw_mcp_tools: Sequence[ToolDef] = (),
    provider_specs: Sequence[SearchProviderSpec] = (),
    turn_timeout_seconds: float | None = None,
    circuit: SearchCircuitBreaker | None = None,
) -> ToolDef | None:
    registry = SearchProviderRegistry.from_tools(
        tools,
        raw_mcp_tools=raw_mcp_tools,
        provider_specs=provider_specs,
    )
    if not registry.available_sources():
        return None
    max_wall = (
        min(turn_timeout_seconds * _SEARCH_BUDGET_RATIO, _MAX_SEARCH_WALL_SECONDS)
        if turn_timeout_seconds is not None
        else _MAX_SEARCH_WALL_SECONDS
    )
    router = SearchRouter(main_llm=main_llm, budget=budget)
    provider = DirectSearchProvider(registry=registry, circuit=circuit)
    page_reader = PageReader(
        web_fetch=registry.tools.get("web_fetch_page"),
        dynamic_browser=registry.tools.get("browse_dynamic_page"),
        max_chars=_MAX_PAGE_SUMMARY_CHARS,
    )
    coordinator = SearchCoordinator(
        router=router,
        registry=registry,
        provider=provider,
        page_reader=page_reader,
        reranker=ResultReranker(router.resolve_llm()),
        max_wall_seconds=max_wall,
    )

    def _handler(args: dict[str, Any]) -> HandlerResult:
        try:
            request = SearchRequest.from_args(args)
        except ValueError as exc:
            payload = {
                "ok": False,
                "error_code": "invalid_search_request",
                "summary": str(exc),
                "results": [],
            }
            return json.dumps(payload, ensure_ascii=False), [], None
        return json.dumps(coordinator.run(request), ensure_ascii=False), [], None

    return ToolDef(
        name="search_information",
        summary=(
            "Unified entry for factual search and URL reading. It routes requests, "
            "checks search-provider health, searches Tavily/Brave/SearXNG or vertical "
            "sources, reads static pages, escalates dynamic pages to browser rendering, "
            "reflects on failures, and returns structured evidence."
        ),
        properties={
            "objective": {
                "type": "string",
                "description": "The concrete factual question or information objective.",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete URLs already known from the user or prior results.",
            },
            "source_hints": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["web", "experience", "commerce", "github", "url"],
                },
                "description": "Explicit logical sources requested by the user.",
            },
            "domain": {
                "type": "string",
                "enum": ["general", "technical", "game", "consumer", "news"],
                "description": (
                    "Query domain hint. 'technical' for APIs/docs/libraries, "
                    "'news' for current events, 'game' for game-related info, "
                    "'consumer' for products/prices."
                ),
                "default": "general",
            },
            "depth": {
                "type": "string",
                "enum": ["quick", "standard", "thorough"],
                "description": (
                    "Search depth. 'quick': single fast search. 'standard': balanced "
                    "search with up to 3 steps. 'thorough': query decomposition, "
                    "up to 5 steps, and result reranking."
                ),
                "default": "standard",
            },
            "time_window": {
                "type": "string",
                "description": "Concrete freshness requirement.",
                "default": "not time-sensitive",
            },
            "required_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Facts every useful result should provide.",
            },
            "verification": {
                "type": "string",
                "enum": ["auto", "required", "none"],
                "default": "auto",
            },
        },
        required=["objective"],
        handler=_handler,
        category="agent.search",
        owner="agent",
        module=__name__,
        artifact_kinds=(),
        weight="heavy",
        metadata={"search_entry": True},
    )


__all__ = ["build_search_tool"]
