"""Project one Bot runtime declaration into the lower-level Agent runtime."""

from __future__ import annotations

from pathlib import Path

import copy
import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from chatcopilot.agent.runtime import AgentRuntime, build_agent_runtime
from chatcopilot.agent.search.providers import DEFAULT_PROVIDER_CREDENTIAL_ENVS
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.botspec.runtime_env import load_research_llm_config, project_resource_roots
from chatcopilot.contracts.runtime import McpServerConfig, RagSourceConfig
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.tool_packs import ToolPackProjectionProfile, ToolProvider
from chatcopilot.core.config import ChatConfig, LLMConfig, load_llm_profile
from chatcopilot.tool_packs.catalog import project_tool_pack_names


class AgentRuntimeAssemblyProfile(str, Enum):
    """Named projections shared by runtime hosts with different trust boundaries."""

    INTERACTIVE = "interactive"
    DETACHED = "detached"


@dataclass(frozen=True)
class AgentRuntimeOverrides:
    """Typed host overrides applied after the selected Bot runtime projection."""

    tool_packs: tuple[str, ...] | None = None
    runtime_providers: tuple[ToolProvider, ...] = ()
    rag_sources: tuple[RagSourceConfig, ...] | None = None
    mcp_servers: tuple[McpServerConfig, ...] | None = None
    subagents: SubagentSpec | None = None
    agent_backend: str | None = None


@dataclass(frozen=True)
class AgentRuntimeProjection:
    """Fully resolved inputs accepted by the Agent-layer runtime factory."""

    chat_config: ChatConfig
    research_llm_config: LLMConfig
    search_llm_config: LLMConfig
    subagent_llm_configs: tuple[tuple[str, LLMConfig], ...]
    search_provider_credentials: tuple[tuple[str, str], ...] = field(repr=False)
    search_quota_max_ttl: float
    tool_packs: tuple[str, ...]
    exclude_tools: tuple[str, ...]
    runtime_providers: tuple[ToolProvider, ...]
    skill_index: tuple[SkillIndexEntry, ...]
    rag_sources: tuple[RagSourceConfig, ...]
    mcp_servers: tuple[McpServerConfig, ...]
    subagents: SubagentSpec
    agent_backend: str
    assembly_profile: ToolPackProjectionProfile
    project_roots: tuple[Path, ...] = ()


def project_agent_runtime(
    runtime: BotRuntimeContext,
    *,
    chat_config: ChatConfig,
    profile: AgentRuntimeAssemblyProfile = AgentRuntimeAssemblyProfile.INTERACTIVE,
    overrides: AgentRuntimeOverrides | None = None,
    environment: Mapping[str, str] | None = None,
) -> AgentRuntimeProjection:
    """Resolve one immutable Bot-to-Agent projection without materializing clients."""

    selected = overrides or AgentRuntimeOverrides()
    env = dict(os.environ if environment is None else environment)
    chat_config = copy.deepcopy(chat_config)
    subagents = copy.deepcopy(runtime.subagents if selected.subagents is None else selected.subagents)
    research_llm_config = load_research_llm_config(
        runtime.spec.llm, fallback=chat_config.llm, environment=env,
    )
    router_prefix = subagents.research_budget.model_env_prefix if subagents.research_enabled else None
    search_llm_config = (
        load_llm_profile(router_prefix, fallback=research_llm_config, environment=env)
        if router_prefix else copy.copy(research_llm_config)
    )
    budgets = [subagents.agents.get(name, subagents.defaults) for name in subagents.include]
    budgets.extend(custom.budget for custom in subagents.custom)
    mcp_servers = tuple(runtime.mcp_servers) if selected.mcp_servers is None else tuple(selected.mcp_servers)
    if any(getattr(server, "risk", "") == "search" for server in mcp_servers):
        budgets.append(subagents.search_budget)
    prefixes = sorted({budget.model_env_prefix for budget in budgets if budget.model_env_prefix})
    quota_max_ttl = float(env.get("CHATCOPILOT_SEARCH_QUOTA_MAX_TTL") or 86400)
    if not math.isfinite(quota_max_ttl) or quota_max_ttl <= 0:
        raise ValueError("CHATCOPILOT_SEARCH_QUOTA_MAX_TTL must be finite and positive")
    candidate_packs = tuple(runtime.tool_packs) if selected.tool_packs is None else tuple(selected.tool_packs)
    projected_packs = project_tool_pack_names(
        candidate_packs,
        profile=profile.value,
    )
    return AgentRuntimeProjection(
        chat_config=chat_config,
        research_llm_config=research_llm_config,
        search_llm_config=search_llm_config,
        subagent_llm_configs=tuple(
            (prefix, load_llm_profile(prefix, fallback=chat_config.llm, environment=env))
            for prefix in prefixes
        ),
        search_provider_credentials=tuple(
            (
                provider.id,
                env.get(
                    provider.credential_env
                    if provider.credential_env is not None
                    else DEFAULT_PROVIDER_CREDENTIAL_ENVS.get(provider.kind, ""),
                    "",
                ).strip(),
            )
            for provider in subagents.search_providers
            if provider.enabled
        ),
        search_quota_max_ttl=quota_max_ttl,
        tool_packs=projected_packs,
        exclude_tools=tuple(runtime.exclude_tools),
        runtime_providers=tuple(selected.runtime_providers),
        skill_index=tuple(runtime.skills),
        rag_sources=(
            tuple(runtime.rag_sources)
            if selected.rag_sources is None
            else tuple(selected.rag_sources)
        ),
        mcp_servers=mcp_servers,
        subagents=subagents,
        agent_backend=(
            str(runtime.agent_backend)
            if selected.agent_backend is None
            else str(selected.agent_backend)
        ),
        assembly_profile=profile.value,
        project_roots=project_resource_roots(runtime.spec, env),
    )


def materialize_agent_runtime(projection: AgentRuntimeProjection) -> AgentRuntime:
    """Build the Agent-layer runtime from one previously resolved projection."""

    return build_agent_runtime(
        chat_config=projection.chat_config,
        research_llm_config=projection.research_llm_config,
        search_llm_config=projection.search_llm_config,
        subagent_llm_configs=projection.subagent_llm_configs,
        search_provider_credentials=projection.search_provider_credentials,
        search_quota_max_ttl=projection.search_quota_max_ttl,
        tool_packs=projection.tool_packs,
        exclude_tools=projection.exclude_tools,
        runtime_providers=projection.runtime_providers,
        skill_index=projection.skill_index,
        rag_sources=projection.rag_sources,
        mcp_servers=projection.mcp_servers,
        subagents=projection.subagents,
        agent_backend=projection.agent_backend,
        assembly_profile=projection.assembly_profile,
        project_roots=projection.project_roots,
    )


def assemble_agent_runtime(
    runtime: BotRuntimeContext,
    *,
    chat_config: ChatConfig,
    profile: AgentRuntimeAssemblyProfile = AgentRuntimeAssemblyProfile.INTERACTIVE,
    overrides: AgentRuntimeOverrides | None = None,
    environment: Mapping[str, str] | None = None,
) -> AgentRuntime:
    """Project and materialize one Agent runtime through the application boundary."""

    return materialize_agent_runtime(
        project_agent_runtime(
            runtime,
            chat_config=chat_config,
            profile=profile,
            overrides=overrides,
            environment=environment,
        )
    )


__all__ = [
    "AgentRuntimeAssemblyProfile",
    "AgentRuntimeOverrides",
    "AgentRuntimeProjection",
    "assemble_agent_runtime",
    "materialize_agent_runtime",
    "project_agent_runtime",
]
