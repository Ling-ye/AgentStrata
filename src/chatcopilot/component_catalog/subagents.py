"""Built-in reusable subagent presets for catalog and Agent runtime."""

from __future__ import annotations

from chatcopilot.contracts.subagents import BUILTIN_SUBAGENT_PRESET_NAMES
from chatcopilot.contracts.subagents import (
    CachePolicySpec,
    ContextPolicySpec,
    PromptLayerSpec,
    SubagentDef,
    ToolMatchRule,
    ToolSelectorSpec,
)

_STRUCTURED_TAIL = (
    "Always finish by calling submit_result. Include evidence, changes, commands_run, "
    "risks, next_steps, confidence, and outputs when they are relevant."
)


def _layers(role: str, *, task_focus: str = "") -> PromptLayerSpec:
    return PromptLayerSpec(
        role=f"{role}\n\n{_STRUCTURED_TAIL}",
        task_focus=task_focus or PromptLayerSpec().task_focus,
    )


_NO_TOOLS = ToolSelectorSpec()
_BROWSER_READ = ToolSelectorSpec(
    any=(
        ToolMatchRule(
            names=(
                "browser_navigate",
                "browser_snapshot",
                "browser_click",
                "browser_hover",
                "browser_navigate_back",
                "browser_wait_for",
                "browser_press_key",
                "browser_tabs",
                "browser_network_requests",
                "browser_close",
            )
        ),
        ToolMatchRule(categories=("web_fetch",)),
    )
)
BUILTIN_SUBAGENTS: dict[str, SubagentDef] = {
    "adapter_forge": SubagentDef(
        name="adapter_forge",
        tool_name="forge_open_source_adapter",
        summary=(
            "Turn an explicitly approved non-MCP open-source repository into a "
            "repository-native AgentStrata external-tool adapter with SDD, tests, docs, "
            "static catalog registration, and focused verification."
        ),
        system_prompt="You are the AgentStrata open-source adapter forge.",
        kind="workflow",
        version="3",
        prompt_layers=_layers(
            "You are the adapter forge. Convert one explicitly approved public "
            "open-source repository into maintained AgentStrata source code; never "
            "dynamically load arbitrary upstream Python into the main process.\n\n"
            "1. Treat source_url, approved_ref, candidate_digest, license_evidence, "
            "integration_intent, and resource_name as the complete Owner-approved "
            "source envelope. Stop if the checked-out source differs.\n"
            "2. Read all applicable AGENTS.md files and trace canonical tool-pack "
            "patterns before editing.\n"
            "3. Create or update an accepted specs/<id>/ before non-trivial "
            "implementation.\n"
            "4. Keep the adapter inside external_tools, share DTOs only through "
            "contracts/core, register through tool_packs.catalog, and avoid cross-layer "
            "imports.\n"
            "5. Add focused tests and update README/AGENTS plus affected architecture "
            "or BotSpec documentation.\n"
            "6. Never download through or invoke LPM, install marketplace resources, "
            "or create a plugin lifecycle record.\n"
            "7. Submit every repository mutation through start_code_task. Never call "
            "direct file-write or shell-mutation tools from this delegate.\n"
            "8. Never git add, commit, push, force-push, or rewrite history. Return the "
            "exact changed files and validation evidence through submit_result."
        ),
        selector=ToolSelectorSpec(
            any=(
                ToolMatchRule(
                    names=(
                        "start_code_task",
                        "read_file",
                        "list_directory",
                        "search_content",
                    )
                ),
                ToolMatchRule(categories=("web_fetch",)),
                ToolMatchRule(categories=("mcp",), mcp_risk=("readonly",)),
            ),
            exclude_names=("git_add", "git_commit", "git_push"),
        ),
        input_schema={
            "source_url": {"type": "string"},
            "approved_ref": {"type": "string"},
            "candidate_digest": {"type": "string"},
            "license_evidence": {"type": "string"},
            "integration_intent": {"type": "string"},
            "resource_name": {"type": "string"},
            "bot": {"type": "string"},
            "_required": [
                "source_url",
                "approved_ref",
                "candidate_digest",
                "license_evidence",
                "integration_intent",
                "resource_name",
            ],
        },
        context_policy=ContextPolicySpec(max_context_tokens=16000, include_history=False),
        cache_policy=CachePolicySpec(enabled=False),
        workflow_tags=("dev", "adapter-forge"),
    ),
    "mcp_query": SubagentDef(
        name="mcp_query",
        tool_name="query_approved_sources",
        summary=(
            "Delegate readonly queries to approved MCP data sources such as GitHub, "
            "Jira, or reasoning helpers. Use when the task needs non-search external "
            "system context. Returns query results without mutating remote state."
        ),
        system_prompt="You are the approved MCP source-query subagent.",
        kind="external",
        version="2",
        prompt_layers=_layers(
            "You are the mcp_query subagent. Query only approved readonly MCP tools. "
            "Search MCP tools belong to dedicated search subagents, not to this subagent. Never write, "
            "create, update, delete, send messages, or mutate remote "
            "state. If the available MCP source is not relevant to the task, return "
            "ok=true with a concise summary explaining that no MCP query was needed."
        ),
        selector=ToolSelectorSpec(
            any=(ToolMatchRule(categories=("mcp",), mcp_risk=("readonly",)),)
        ),
        cache_policy=CachePolicySpec(enabled=True, ttl_seconds=300, namespace="mcp_query"),
        unavailable_message=(
            "mcp_query_unavailable: no approved readonly MCP tool is available "
            "for mcp_query."
        ),
    ),
    "developer": SubagentDef(
        name="developer",
        tool_name="delegate_development",
        summary=(
            "Delegate a development task: understand codebase, plan approach, "
            "implement changes, verify correctness, and prepare a handoff. "
            "Use for multi-file changes or tasks requiring testing."
        ),
        system_prompt="You are a software development agent operating directly on the working directory.",
        kind="workflow",
        version="1",
        prompt_layers=_layers(
            "You are an autonomous software developer. You operate directly on the working "
            "directory.\n\n"
            "## Adaptive Development Protocol\n\n"
            "### Phase 1: Understand (always)\n"
            "- search_content / list_directory / read_file to grasp context\n"
            "- For unfamiliar code: trace call chains before modifying\n\n"
            "### Phase 2: Plan (non-trivial tasks)\n"
            "- State your approach in 3-5 bullet points\n"
            "- Identify affected files, dependencies, and risks\n"
            "- Skip for trivial (typo, single-line fix)\n\n"
            "### Phase 3: Implement\n"
            "- edit_file for modifications (preferred - fuzzy search-replace)\n"
            "- write_file for new files\n"
            "- Work incrementally; verify each significant edit with read_file\n\n"
            "### Phase 4: Verify (always for code changes)\n"
            "- run_command: python -m compileall <files>  (at minimum)\n"
            "- run_command: python -m pytest tests/unit -q -k <relevant>  (when tests exist)\n"
            "- Fix failures; up to 3 fix-verify cycles, then report in risks\n\n"
            "### Phase 5: Handoff (when objective is met)\n"
            "- Use git_status and git_diff only to enumerate your exact changes and preserve "
            "all pre-existing dirty files.\n"
            "- Do not git add, commit, push, force-push, or rewrite history; the main agent "
            "hands uncommitted changes to the owner for review.\n\n"
            "## Complexity Adaptation\n"
            "- Trivial: Phase 1 → 3 → 5\n"
            "- Medium: Phase 1 → 2(brief) → 3 → 4 → 5\n"
            "- Large: Full protocol, multiple 3→4 cycles\n\n"
            "## Safety Rules\n"
            "- Never stage or commit; do not claim pre-existing dirty files as your changes\n"
            "- Never modify files outside write_scope / allowed_paths\n"
            "- Delegated run_command accepts validation commands only; use file tools for edits\n"
            "- If a test breaks unrelated code, stop and report in risks"
        ),
        selector=ToolSelectorSpec(
            any=(
                ToolMatchRule(category_prefixes=("dev.",)),
                ToolMatchRule(mcp_risk=("write",)),
            ),
            exclude_names=("git_add", "git_commit", "git_push"),
        ),
        context_policy=ContextPolicySpec(max_context_tokens=12000, include_history=False),
        cache_policy=CachePolicySpec(enabled=False),
        workflow_tags=("dev",),
    ),
    "browser_reader": SubagentDef(
        name="browser_reader",
        tool_name="browse_dynamic_page",
        summary=(
            "Read a JavaScript-rendered webpage with limited browser interaction. "
            "Use only after static URL fetching is insufficient."
        ),
        system_prompt="You are the dynamic webpage reading subagent.",
        kind="external",
        version="1",
        prompt_layers=_layers(
            "Open the concrete URL from the task resources or inputs. Read the rendered "
            "page using accessibility snapshots. You may click links or expanders, hover, "
            "scroll with PageDown/PageUp/Home/End, wait briefly, and manage existing tabs. "
            "Do not type into fields, submit forms, upload or download files, authenticate, "
            "or execute arbitrary JavaScript. Prefer a public GET JSON API only when its URL "
            "is explicitly visible in the page's network request list; fetch that URL with "
            "web_fetch_page. Return the final URL, extracted facts, and interactions performed. "
            "The runner closes the browser after the task, so do not rely on browser state "
            "persisting across tasks.\n\n"
            "EARLY EXIT: If you encounter a CAPTCHA, login wall, anti-bot challenge, "
            "cookie consent that blocks content, or any authentication gate that prevents "
            "reading the page, call submit_result IMMEDIATELY with ok=false and "
            "error_code='browser_blocked'. Do NOT retry or waste iterations on blocked pages."
        ),
        selector=_BROWSER_READ,
        cache_policy=CachePolicySpec(enabled=False),
        cleanup_tools=("browser_close",),
        unavailable_message=(
            "browser_reader_unavailable: the approved Playwright browser source is not connected."
        ),
    ),
}

PRESET_NAMES = BUILTIN_SUBAGENT_PRESET_NAMES
assert PRESET_NAMES == frozenset(BUILTIN_SUBAGENTS)


__all__ = ["BUILTIN_SUBAGENTS", "PRESET_NAMES", "SubagentDef"]
