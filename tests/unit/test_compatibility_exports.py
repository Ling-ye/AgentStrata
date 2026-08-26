from __future__ import annotations

from typing import Any, cast

import pytest


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


def test_contracts_root_exports_canonical_agent_backend_ids() -> None:
    from chatcopilot.contracts import AGENT_BACKEND_IDS as root_backend_ids
    from chatcopilot.contracts.agent_backend import AGENT_BACKEND_IDS

    assert root_backend_ids is AGENT_BACKEND_IDS


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


def test_legacy_mcp_admin_tools_alias_canonical_provider() -> None:
    from chatcopilot.agent.tools.builtin.mcp_tools import TOOLS as legacy_tools
    from chatcopilot.external_tools.mcp_admin.tools import TOOLS

    assert legacy_tools is TOOLS


def test_attachment_detection_compatibility_name_keeps_canonical_behavior() -> None:
    from chatcopilot.middleware.acp.attachment_pipeline import (
        has_text_attachment_reference,
        looks_like_attachment_upload_text,
    )

    for text in ("请看附件 report.txt", "https://example.com/report.txt"):
        assert looks_like_attachment_upload_text(text) is has_text_attachment_reference(text)


def test_canonical_subagent_catalog_is_immutable() -> None:
    from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS

    with pytest.raises(TypeError):
        cast(dict[str, Any], BUILTIN_SUBAGENTS)["injected"] = object()


def test_component_catalog_exposes_control_plane_dtos() -> None:
    from chatcopilot.component_catalog import (
        get_subagent_preset,
        get_workflow,
        iter_subagent_presets,
        iter_tool_features,
        iter_tool_packs,
        iter_workflows,
        known_subagent_preset_names,
        known_workflow_names,
    )
    from chatcopilot.contracts.subagents import (
        BUILTIN_SUBAGENT_PRESET_NAMES,
        BUILTIN_SUBAGENT_WORKFLOW_NAMES,
        BUILTIN_SUBAGENT_WORKFLOWS,
    )

    tool_pack_names = {name for name, _ in iter_tool_packs()}
    preset_records = list(iter_subagent_presets())
    preset_names = {name for name, _ in preset_records}
    feature_names = {name for name, _ in iter_tool_features()}

    assert "workspace.read_write" in tool_pack_names
    assert "developer" in preset_names
    assert "chat.file_uploads" in feature_names
    assert known_subagent_preset_names() == frozenset(preset_names)
    assert get_subagent_preset("developer") is dict(preset_records)["developer"]
    assert get_subagent_preset("missing") is None
    assert BUILTIN_SUBAGENT_PRESET_NAMES == known_subagent_preset_names()
    assert known_workflow_names() == frozenset()
    assert get_workflow("missing") is None
    assert list(iter_workflows()) == []
    assert BUILTIN_SUBAGENT_WORKFLOW_NAMES == known_workflow_names()
    assert BUILTIN_SUBAGENT_WORKFLOWS == dict(iter_workflows())
