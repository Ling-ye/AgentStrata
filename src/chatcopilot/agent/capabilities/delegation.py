"""Session contributor for subagent and workflow delegation tools."""
from __future__ import annotations

from chatcopilot.agent.capabilities.assembly import SessionCapabilityContext
from chatcopilot.agent.subagents.registry import build_subagent_provider
from chatcopilot.contracts.tool_packs import ToolProvider


def build_provider(
    context: SessionCapabilityContext,
) -> ToolProvider | None:
    direct_codex = context.backend_id == "codex"
    allow_all = direct_codex and context.subagents.codex.allow_delegate_tools
    if direct_codex and not allow_all and "adapter_forge" not in context.subagents.include:
        return None

    provider = build_subagent_provider(
        session_id=context.session_id,
        subagents=context.subagents,
        main_llm=context.main_llm,
        main_config=context.runtime_config,
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
    if provider is None or not direct_codex or allow_all:
        return provider

    tools = tuple(
        tool
        for tool in provider.packs["agent.delegation"]
        if tool.metadata.get("subagent") == "adapter_forge"
    )
    if not tools:
        return None
    return ToolProvider(
        id=provider.id,
        packs={"agent.delegation": tools},
        module=provider.module,
        description=provider.description,
    )


__all__ = ["build_provider"]
