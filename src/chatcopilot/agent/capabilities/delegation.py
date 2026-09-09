"""Session contributor for subagent and workflow delegation tools."""
from __future__ import annotations

from chatcopilot.agent.capabilities.assembly import SessionCapabilityContext
from chatcopilot.agent.subagents.registry import build_subagent_provider
from chatcopilot.contracts.tool_packs import ToolProvider


def build_provider(
    context: SessionCapabilityContext,
) -> ToolProvider | None:
    provider = build_subagent_provider(
        session_id=context.session_id,
        subagents=context.subagents,
        main_llm=context.main_llm,
        main_config=context.runtime_config,
        llm_profiles=context.subagent_llms,
        base_tools=context.subagent_tools or context.base_tools,
        mcp_configs=context.mcp_configs,
        background_submitter=context.background_submitter,
        permission_filter=context.permission_filter,
        file_sender=context.file_sender,
        workspace_service=context.workspace_service,
        memory_snapshot=context.memory_snapshot,
        retriever=context.retriever,
        search_circuit=context.search_circuit,
    )
    return provider


__all__ = ["build_provider"]
