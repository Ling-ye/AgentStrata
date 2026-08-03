"""Search subagent definitions generated from MCP search servers."""

from __future__ import annotations

from typing import Sequence

from chatcopilot.agent.search_policy import SEARCH_TASK_PROPERTIES, SEARCH_TASK_REQUIRED_FIELDS
from chatcopilot.agent.subagents.runner import SubagentRuntimeConfig
from chatcopilot.agent.subagents.spec import (
    CachePolicySpec,
    PromptLayerSpec,
    SubagentDef,
    ToolMatchRule,
    ToolSelectorSpec,
)
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.subagents import SubagentBudgetSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_SEARCH_SUBAGENT_PREFIX = "search_"

_SEARCH_PROMPT_TAIL = (
    "Always finish by calling submit_result. Include evidence, "
    "risks, next_steps, confidence, and outputs when they are relevant."
)


def build_search_prompt(config: McpServerConfig) -> PromptLayerSpec:
    server_id = config.id
    role = (
        f"You are the search subagent for '{server_id}'. "
        "Use the provided search tools to find relevant information. "
        "Follow the task pack's domain, target_sites, time_window, required_fields, "
        "and cross_check constraints. Include source dates and links, and flag uncertainty."
        f"{render_search_source_policy(config)}\n\n"
        f"{_SEARCH_PROMPT_TAIL}"
    )
    task_focus = (
        "DEPTH-AWARE EXECUTION PLAN:\n"
        "Check the task pack's `depth` field to determine your search budget:\n\n"
        "depth=quick:\n"
        "  1. Build a precise query, call the search tool ONCE\n"
        "  2. Call submit_result immediately — no deep reads\n\n"
        "depth=standard (default if depth is absent):\n"
        "  1. Build a precise query from objective, domain, target_sites, and time_window\n"
        "  2. Call the search tool once\n"
        "  3. If results are irrelevant or empty, rewrite the query once and search again\n"
        "  4. If a specific fact needs page detail, fetch only the single best URL\n"
        "  5. Call submit_result — at most 2 searches + 1 deep read\n\n"
        "depth=thorough:\n"
        "  1. Build an initial query, call the search tool\n"
        "  2. Analyze results; if gaps remain, refine the query and search again\n"
        "  3. If still insufficient, try a third query variation or broader terms\n"
        "  4. Deep-read up to 2 promising URLs for detailed facts\n"
        "  5. Call submit_result — at most 3 searches + 2 deep reads\n\n"
        "DOMAIN-SPECIFIC STRATEGY:\n"
        "- technical: target official docs (site:docs.*, site:developer.*, site:*.readthedocs.io);\n"
        "  include version numbers in queries; prefer search_then_read for API details;\n"
        "  required evidence: exact function/class names, parameter types, return values\n"
        "- news: add date range or 'latest'/'2026' to queries; prefer most recent;\n"
        "  required evidence: source name, publication date, key facts\n"
        "- game: prefer official wiki (site:*.fandom.com, site:*.wiki.gg) and patch notes;\n"
        "  required evidence: version/patch, mechanic details, source type\n"
        "- consumer: focus on specs, verified prices, user ratings;\n"
        "  required evidence: price, platform, key specs, rating if available\n\n"
        "QUERY RULES:\n"
        "- Prefer user-requested sites, then official/primary sources, then reputable secondary sources\n"
        "- Pass preferred/excluded domain parameters when the tool schema supports them\n"
        "- Otherwise express target/preferred sites with site:hostname and excluded sites "
        "with -site:hostname query terms\n"
        "- Do not claim cross-source verification; the main agent owns second-source calls\n"
        "- For game/anime/ACG topics: prefer English keywords for the initial query "
        "(e.g. 'Escape from Tarkov textbook spawn' instead of '逃离塔科夫 教材'); "
        "if the first search yields irrelevant results, retry with the other language\n\n"
        "RESULT CONTRACT:\n"
        "- For each useful result return title, URL, published/updated date when available, "
        "source type, and the required facts\n"
        "- State explicitly when a required field or date is unavailable\n"
        "- Keep unsupported claims out of evidence\n\n"
        "BUDGET RULES (strictly follow):\n"
        "- Reserve your LAST tool call for submit_result — always\n"
        "- If you receive a budget warning, submit_result is your only allowed next action\n"
        "- Partial results with high confidence > exhaustive results over budget\n"
        "- Do NOT output reasoning text between search and submit — act immediately"
    )
    task_focus += (
        "\n\nIMAGE REQUESTS:\n"
        "- If the task asks to search/send/find images, preserve direct image URLs "
        "from image_candidates or tool outputs in submit_result.outputs, up to 5 URLs\n"
        "- For Tavily/web search tools, pass include_images=true when the schema supports it; "
        "include_image_descriptions=true when useful\n"
        "- If you found relevant text but no direct image URL, say so explicitly; "
        "do not invent URLs"
    )
    task_focus += (
        "\n\nURL DEEP FETCH:\n"
        "- If search results contain a promising URL but the snippet is insufficient "
        "to answer the question, use web_fetch_page to retrieve the full page content\n"
        "- For depth=quick: no deep reads allowed\n"
        "- For depth=standard: fetch at most 1 URL\n"
        "- For depth=thorough: fetch at most 2 URLs — pick the most relevant results\n"
        "- Do NOT fetch URLs speculatively; only when the snippet clearly lacks the needed detail"
    )
    task_focus += (
        "\n\nINFRASTRUCTURE ERROR — EARLY EXIT:\n"
        "- If your primary search tool returns quota_exceeded, unavailable, rate_limit, "
        "mcp_quota_exceeded, mcp_unavailable, or similar infrastructure errors, "
        "call submit_result(ok=false, error_code='<the_error_code>') IMMEDIATELY.\n"
        "- Do NOT attempt to guess URLs, construct URLs manually, or use web_fetch_page "
        "as a workaround when the search tool itself is down.\n"
        "- Do NOT retry the search tool after an infrastructure error — it will fail again."
    )
    return PromptLayerSpec(role=role, task_focus=task_focus)


def build_search_subagent(
    source_config: McpServerConfig, budget: SubagentBudgetSpec
) -> tuple[SubagentDef, SubagentRuntimeConfig]:
    server_id = source_config.id
    name = f"{_SEARCH_SUBAGENT_PREFIX}{server_id}"
    summary = source_config.search_summary or f"Search '{server_id}' for information."
    definition = SubagentDef(
        name=name,
        tool_name=name,
        summary=f"Delegate search to {server_id}. {summary}",
        system_prompt=f"You are the {server_id} search subagent.",
        kind="search",
        version="2",
        prompt_layers=build_search_prompt(source_config),
        selector=ToolSelectorSpec(
            any=(
                ToolMatchRule(categories=("mcp",), owners=(server_id,)),
                ToolMatchRule(categories=("web_fetch",)),
            )
        ),
        input_schema={
            **SEARCH_TASK_PROPERTIES,
            "_required": list(SEARCH_TASK_REQUIRED_FIELDS),
        },
        cache_policy=CachePolicySpec(enabled=True, ttl_seconds=1200, namespace=name),
        unavailable_message=f"{name}_unavailable: {server_id} MCP is not connected.",
    )
    runtime_config = SubagentRuntimeConfig(
        model_env_prefix=budget.model_env_prefix,
        max_model_turns=budget.max_model_turns,
        max_tool_calls=budget.max_tool_calls,
        timeout_seconds=budget.timeout_seconds,
        max_output_chars=budget.max_output_chars,
    )
    return definition, runtime_config


def render_search_source_policy(config: McpServerConfig) -> str:
    lines: list[str] = []
    if config.search_domain_guidance:
        lines.append(f"\nSource domain guidance: {config.search_domain_guidance}")
    if config.preferred_domains:
        lines.append(
            "\nPreferred domains when relevant: " + ", ".join(config.preferred_domains)
        )
    if config.excluded_domains:
        lines.append("\nExcluded domains: " + ", ".join(config.excluded_domains))
    return "".join(lines)


def collect_search_servers(
    base_tools: Sequence[ToolDef],
    mcp_configs: Sequence[McpServerConfig],
) -> dict[str, McpServerConfig]:
    """Return MCP server configs that should produce auto-generated search subagents."""
    search_server_ids: set[str] = set()
    for tool in base_tools:
        if tool.category == "mcp" and str(tool.metadata.get("mcp_risk", "")) == "search":
            sid = str(tool.metadata.get("mcp_server_id", "")).strip()
            if sid:
                search_server_ids.add(sid)
    configs_by_id = {cfg.id: cfg for cfg in mcp_configs if cfg.risk == "search"}
    return {sid: configs_by_id[sid] for sid in search_server_ids if sid in configs_by_id}


__all__ = [
    "build_search_prompt",
    "build_search_subagent",
    "collect_search_servers",
    "render_search_source_policy",
]
