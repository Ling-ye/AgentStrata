from __future__ import annotations


def test_legacy_runtime_exports_alias_core_runtime_modules() -> None:
    from chatcopilot.agent.config import ChatConfig as LegacyChatConfig
    from chatcopilot.agent.llm_client import LLMClient as LegacyLLMClient
    from chatcopilot.botspec.mcp_catalog import load_mcp_catalog as legacy_load_mcp_catalog
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.llm_client import LLMClient
    from chatcopilot.core.mcp_catalog import load_mcp_catalog

    assert LegacyChatConfig is ChatConfig
    assert LegacyLLMClient is LLMClient
    assert legacy_load_mcp_catalog is load_mcp_catalog


def test_legacy_agent_protocol_exports_alias_contracts() -> None:
    from chatcopilot.agent import protocol as legacy_protocol
    from chatcopilot.contracts import agent as contracts_agent

    assert legacy_protocol.__all__ == contracts_agent.__all__
    for name in contracts_agent.__all__:
        assert getattr(legacy_protocol, name) is getattr(contracts_agent, name)


def test_legacy_research_exports_alias_canonical_search_types() -> None:
    from chatcopilot.agent.research.models import ResearchRequest
    from chatcopilot.agent.research.router import ResearchRouter
    from chatcopilot.agent.research.runtime import build_research_tool
    from chatcopilot.agent.search.models import SearchRequest
    from chatcopilot.agent.search.router import SearchRouter

    assert ResearchRequest is SearchRequest
    assert ResearchRouter is SearchRouter
    assert callable(build_research_tool)


def test_legacy_subagent_presets_alias_component_catalog() -> None:
    from chatcopilot.agent.subagents.presets import BUILTIN_SUBAGENTS as legacy_presets
    from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS

    assert legacy_presets is BUILTIN_SUBAGENTS


def test_component_catalog_exposes_control_plane_dtos() -> None:
    from chatcopilot.component_catalog import iter_subagent_presets, iter_tool_features, iter_tool_packs, iter_workflows

    tool_pack_names = {name for name, _ in iter_tool_packs()}
    preset_names = {name for name, _ in iter_subagent_presets()}
    feature_names = {name for name, _ in iter_tool_features()}

    assert "workspace.read_write" in tool_pack_names
    assert "developer" in preset_names
    assert "chat.file_uploads" in feature_names
    assert list(iter_workflows()) == []
