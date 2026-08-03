"""Neutral runtime plan DTOs produced by configuration and consumed by assembly."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class McpServerConfig:
    id: str
    transport: str = "stdio"
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    artifact_digest: str = ""
    tool_prefix: str = ""
    exposure: str = "subagent"
    allowed_subagents: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    risk: str = "search"
    timeout_seconds: float = 30.0
    max_result_chars: int = 20000
    retry_on_timeout: bool = False
    max_concurrency: int = 0
    stateless_http: bool = False
    search_summary: str = ""
    search_only_tools: tuple[str, ...] = ()
    preferred_domains: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    search_domain_guidance: str = ""


@dataclass(frozen=True)
class RagSourceConfig:
    path: Path
    label: str = ""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_chunk_chars: int = 1200


McpPlan = McpServerConfig
RagSourcePlan = RagSourceConfig


@dataclass(frozen=True)
class SubagentBudgetPlan:
    model_env_prefix: str | None = None
    max_model_turns: int = 3
    max_tool_calls: int = 6
    timeout_seconds: int = 120
    max_output_chars: int = 6000


@dataclass(frozen=True)
class SubagentPlan:
    backend: str = "native"
    include: tuple[str, ...] = ()
    defaults: SubagentBudgetPlan = field(default_factory=SubagentBudgetPlan)
    search_budget: SubagentBudgetPlan = field(default_factory=SubagentBudgetPlan)
    research_enabled: bool = False
    research_budget: SubagentBudgetPlan = field(default_factory=SubagentBudgetPlan)
    agents: dict[str, SubagentBudgetPlan] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    custom: tuple[Any, ...] = ()
    workflows: tuple[str, ...] = ()
    max_workflow_depth: int = 2


@dataclass(frozen=True)
class AgentRuntimePlan:
    backend: str = "native"
    tool_packs: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    mcp_servers: tuple[McpServerConfig, ...] = ()
    rag_sources: tuple[RagSourceConfig, ...] = ()
    subagents: Any = None


@dataclass(frozen=True)
class BotRuntimePlan:
    bot_id: str
    instance_id: str
    display_name: str
    platform_type: str
    platform_adapter: str
    agent: AgentRuntimePlan
    source_path: Path


__all__ = [
    "AgentRuntimePlan",
    "BotRuntimePlan",
    "McpPlan",
    "McpServerConfig",
    "RagSourceConfig",
    "RagSourcePlan",
    "SubagentBudgetPlan",
    "SubagentPlan",
]
