"""Declarative subagent contracts shared by BotSpec and Agent.

A subagent is described by what role it plays, what tools it may touch, and
how much budget it gets. The tool whitelist is data, so presets and BotSpec
custom entries share one mechanism.

Matching semantics:

- A :class:`ToolSelectorSpec` selects a tool when **any** of its rules match
  (OR across rules) and the tool is not user-facing / not explicitly excluded.
- Within one :class:`ToolMatchRule`, **every non-empty** field must match (AND
  within a rule). Empty fields are ignored.

The selector only governs schema exposure / candidacy. Hard safety filters
(user-facing ban, MCP exposure & allowed_subagents, role permission_filter) are
intersected on top by the registry and runner — a selector can never widen them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chatcopilot.contracts.agent_backend import CodexMainSessionPolicy


TASK_PACK_FIELDS: tuple[str, ...] = (
    "objective",
    "user_intent",
    "deliverable",
    "constraints",
    "inputs",
    "resources",
    "acceptance_criteria",
    "evidence_required",
    "domain",
    "target_sites",
    "time_window",
    "required_fields",
    "cross_check",
    "write_scope",
    "excluded_context",
    "cache_key_hint",
)

SUBAGENT_RESULT_FIELDS: tuple[str, ...] = (
    "ok",
    "error_code",
    "summary",
    "findings",
    "evidence",
    "changes",
    "commands_run",
    "outputs",
    "risks",
    "next_steps",
    "confidence",
    "cache_summary",
)


@dataclass(frozen=True)
class ToolMatchRule:
    """One conjunctive matching rule over a :class:`ToolDef`'s descriptive fields.

    Every non-empty field is ANDed together. ``mcp_risk`` matches against
    ``ToolDef.metadata['mcp_risk']``; ``tags`` against ``metadata['tags']``.
    """

    names: tuple[str, ...] = ()
    name_prefixes: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    category_prefixes: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    module_prefixes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    mcp_risk: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.names,
                self.name_prefixes,
                self.categories,
                self.category_prefixes,
                self.owners,
                self.module_prefixes,
                self.tags,
                self.mcp_risk,
            )
        )


@dataclass(frozen=True)
class ToolSelectorSpec:
    """Declarative tool whitelist for one subagent.

    ``any`` is OR'd across rules. ``exclude_names`` is a hard blacklist applied
    after matching. An empty ``any`` selects nothing (fail-closed).
    """

    any: tuple[ToolMatchRule, ...] = ()
    exclude_names: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.any


@dataclass(frozen=True)
class PromptLayerSpec:
    """Cache-friendly prompt layers for one subagent.

    Stable layers should stay before dynamic task-specific text so model-side
    prompt caching can reuse the longest common prefix across calls.
    """

    framework_base: str = (
        "You are an internal AgentStrata subagent. The main agent owns all user "
        "interaction, final delivery, and task accountability. Return only "
        "structured results through submit_result.\n\n"
        "## Execution Protocol\n\n"
        "1. Parse the task pack (objective, constraints, acceptance_criteria).\n"
        "2. Use allowed tools iteratively to gather evidence or perform actions.\n"
        "3. When done (or budget nearly exhausted), call submit_result with:\n"
        "   - ok: bool — whether objective was met\n"
        "   - summary: concise answer in natural language (required)\n"
        "   - findings: list of key facts or observations\n"
        "   - evidence: list of {claim, source} pairs\n"
        "   - changes: list of mutations made (file writes, commits)\n"
        "   - outputs: list of artifact paths produced\n"
        "   - risks: anything the main agent should know\n"
        "   - next_steps: suggested follow-ups if incomplete\n"
        "   - confidence: low / medium / high\n\n"
        "## Tool Constraints\n\n"
        "- Only call tools listed in your schema; never fabricate tool names.\n"
        "- Respect write_scope boundaries; read-only tasks must not mutate state.\n"
        "- If a tool fails, retry once with adjusted params, then report in risks.\n"
        "- Never produce user-facing output directly; all communication goes via "
        "submit_result summary."
    )
    role: str = ""
    bot_override: str = ""
    task_focus: str = (
        "Use only the provided task pack, allowed tools, and explicitly referenced "
        "resources. Treat excluded_context as information that must not influence "
        "the answer."
    )
    safety_tail: str = (
        "Do not call user-facing tools. Stay within the write_scope if one is "
        "provided. If the task cannot be completed safely, return ok=false with "
        "risks and next_steps."
    )


@dataclass(frozen=True)
class ContextPolicySpec:
    """Rules for building the short subagent context pack."""

    max_context_tokens: int = 8000
    sliding_window_turns: int = 1
    include_tool_summary: bool = True
    include_history: bool = False
    include_allowed_tools: bool = True
    allowed_task_fields: tuple[str, ...] = TASK_PACK_FIELDS


@dataclass(frozen=True)
class CachePolicySpec:
    """Subtask result cache policy."""

    enabled: bool = False
    ttl_seconds: int = 0
    include_resource_hashes: bool = True
    namespace: str = "default"


@dataclass(frozen=True)
class WorkflowDef:
    """A deterministic workflow made of existing subagent names.

    ``retry_map`` maps a failing step name to the step it should retry from.
    For example ``(("code_reviewer", "code_implementer"),)`` means: if reviewer
    fails, re-run from implementer with the failure context injected.
    """

    name: str
    tool_name: str
    summary: str
    steps: tuple[str, ...]
    optional_steps: tuple[str, ...] = ()
    retry_map: tuple[tuple[str, str], ...] = ()
    max_retries: int = 1
    max_depth: int = 2


@dataclass(frozen=True)
class SubagentDef:
    """A fully resolved subagent ready to be wrapped as a delegate tool.

    ``system_prompt`` is inline text (preset) or already-resolved from a BotSpec
    pointer. ``selector`` is compiled into a predicate by ``selector.py``.
    """

    name: str
    tool_name: str
    summary: str
    system_prompt: str
    kind: str = "domain"
    version: str = "1"
    prompt_layers: PromptLayerSpec = field(default_factory=PromptLayerSpec)
    selector: ToolSelectorSpec = field(default_factory=ToolSelectorSpec)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    context_policy: ContextPolicySpec = field(default_factory=ContextPolicySpec)
    cache_policy: CachePolicySpec = field(default_factory=CachePolicySpec)
    workflow_tags: tuple[str, ...] = ()
    cleanup_tools: tuple[str, ...] = ()
    unavailable_message: str | None = None


@dataclass(frozen=True)
class SubagentBudgetSpec:
    """Per-subagent execution budget."""

    model_env_prefix: str | None = None
    max_model_turns: int = 3
    max_tool_calls: int = 6
    timeout_seconds: int = 120
    max_output_chars: int = 6000


@dataclass(frozen=True)
class SearchProviderSpec:
    """Non-secret policy for one in-process unified-search provider.

    ``credential_env`` names a machine environment variable; its value is never
    part of the BotSpec contract.  ``endpoint`` may be omitted to use the
    reviewed default for the selected provider kind.
    """

    id: str
    kind: str
    enabled: bool = True
    endpoint: str | None = None
    credential_env: str | None = None
    timeout_seconds: float = 15.0
    max_results: int = 10


@dataclass(frozen=True)
class CustomSubagentSpec:
    """A BotSpec-declared custom subagent, resolved before Agent runtime use."""

    name: str
    tool_name: str
    summary: str
    selector: ToolSelectorSpec
    budget: SubagentBudgetSpec = field(default_factory=SubagentBudgetSpec)
    prompt_path: str | None = None
    system_prompt: str = ""
    kind: str = "domain"
    version: str = "1"
    prompt_layers: PromptLayerSpec = field(default_factory=PromptLayerSpec)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    context_policy: ContextPolicySpec = field(default_factory=ContextPolicySpec)
    cache_policy: CachePolicySpec = field(default_factory=CachePolicySpec)
    workflow_tags: tuple[str, ...] = ()
    override_fields: tuple[str, ...] = ()
    unavailable_message: str | None = None


@dataclass(frozen=True)
class SubagentSpec:
    """Bot-level subagent configuration."""

    backend: str = "native"
    include: tuple[str, ...] = ()
    defaults: SubagentBudgetSpec = field(default_factory=SubagentBudgetSpec)
    search_budget: SubagentBudgetSpec = field(default_factory=SubagentBudgetSpec)
    research_enabled: bool = False
    research_budget: SubagentBudgetSpec = field(default_factory=SubagentBudgetSpec)
    search_providers: tuple[SearchProviderSpec, ...] = ()
    agents: dict[str, SubagentBudgetSpec] = field(default_factory=dict)
    overrides: dict[str, CustomSubagentSpec] = field(default_factory=dict)
    custom: tuple[CustomSubagentSpec, ...] = ()
    workflows: tuple[str, ...] = ()
    max_workflow_depth: int = 2
    codex: CodexMainSessionPolicy = field(default_factory=CodexMainSessionPolicy)


BUILTIN_SUBAGENT_PRESET_NAMES: frozenset[str] = frozenset((
    "adapter_forge",
    "browser_reader",
    "developer",
    "mcp_query",
))

BUILTIN_SUBAGENT_WORKFLOWS: dict[str, WorkflowDef] = {}
BUILTIN_SUBAGENT_WORKFLOW_NAMES: frozenset[str] = frozenset(BUILTIN_SUBAGENT_WORKFLOWS)


__all__ = [
    "BUILTIN_SUBAGENT_PRESET_NAMES",
    "BUILTIN_SUBAGENT_WORKFLOW_NAMES",
    "BUILTIN_SUBAGENT_WORKFLOWS",
    "CachePolicySpec",
    "CodexMainSessionPolicy",
    "ContextPolicySpec",
    "PromptLayerSpec",
    "SearchProviderSpec",
    "CustomSubagentSpec",
    "SubagentBudgetSpec",
    "SubagentSpec",
    "SubagentDef",
    "TASK_PACK_FIELDS",
    "SUBAGENT_RESULT_FIELDS",
    "ToolMatchRule",
    "ToolSelectorSpec",
    "WorkflowDef",
]
