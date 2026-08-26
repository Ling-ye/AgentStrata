"""Project one Bot runtime declaration into the lower-level Agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from chatcopilot.agent.runtime import AgentRuntime, build_agent_runtime
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.botspec.runtime_env import load_research_llm_config
from chatcopilot.contracts.runtime import McpServerConfig, RagSourceConfig
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.tool_packs import ToolPackProjectionProfile, ToolProvider
from chatcopilot.core.config import ChatConfig, LLMConfig
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
    tool_packs: tuple[str, ...]
    exclude_tools: tuple[str, ...]
    runtime_providers: tuple[ToolProvider, ...]
    skill_index: tuple[SkillIndexEntry, ...]
    rag_sources: tuple[RagSourceConfig, ...]
    mcp_servers: tuple[McpServerConfig, ...]
    subagents: SubagentSpec
    agent_backend: str
    assembly_profile: ToolPackProjectionProfile


def project_agent_runtime(
    runtime: BotRuntimeContext,
    *,
    chat_config: ChatConfig,
    profile: AgentRuntimeAssemblyProfile = AgentRuntimeAssemblyProfile.INTERACTIVE,
    overrides: AgentRuntimeOverrides | None = None,
) -> AgentRuntimeProjection:
    """Resolve one immutable Bot-to-Agent projection without materializing clients."""

    selected = overrides or AgentRuntimeOverrides()
    candidate_packs = tuple(runtime.tool_packs) if selected.tool_packs is None else tuple(selected.tool_packs)
    projected_packs = project_tool_pack_names(
        candidate_packs,
        profile=profile.value,
    )
    return AgentRuntimeProjection(
        chat_config=chat_config,
        research_llm_config=load_research_llm_config(
            runtime.spec.llm,
            fallback=chat_config.llm,
        ),
        tool_packs=projected_packs,
        exclude_tools=tuple(runtime.exclude_tools),
        runtime_providers=tuple(selected.runtime_providers),
        skill_index=tuple(runtime.skills),
        rag_sources=(
            tuple(runtime.rag_sources)
            if selected.rag_sources is None
            else tuple(selected.rag_sources)
        ),
        mcp_servers=(
            tuple(runtime.mcp_servers)
            if selected.mcp_servers is None
            else tuple(selected.mcp_servers)
        ),
        subagents=runtime.subagents if selected.subagents is None else selected.subagents,
        agent_backend=(
            str(runtime.agent_backend)
            if selected.agent_backend is None
            else str(selected.agent_backend)
        ),
        assembly_profile=profile.value,
    )


def materialize_agent_runtime(projection: AgentRuntimeProjection) -> AgentRuntime:
    """Build the Agent-layer runtime from one previously resolved projection."""

    return build_agent_runtime(
        chat_config=projection.chat_config,
        research_llm_config=projection.research_llm_config,
        tool_packs=projection.tool_packs,
        exclude_tools=projection.exclude_tools,
        runtime_providers=projection.runtime_providers,
        skill_index=projection.skill_index,
        rag_sources=projection.rag_sources,
        mcp_servers=projection.mcp_servers,
        subagents=projection.subagents,
        agent_backend=projection.agent_backend,
        assembly_profile=projection.assembly_profile,
    )


def assemble_agent_runtime(
    runtime: BotRuntimeContext,
    *,
    chat_config: ChatConfig,
    profile: AgentRuntimeAssemblyProfile = AgentRuntimeAssemblyProfile.INTERACTIVE,
    overrides: AgentRuntimeOverrides | None = None,
) -> AgentRuntime:
    """Project and materialize one Agent runtime through the application boundary."""

    return materialize_agent_runtime(
        project_agent_runtime(
            runtime,
            chat_config=chat_config,
            profile=profile,
            overrides=overrides,
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
