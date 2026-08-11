from __future__ import annotations

from types import ModuleType

import pytest

from chatcopilot.agent.tools.builtin import BUILTIN_TOOL_MODULES_BY_TOOL_PACK
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.component_catalog import CatalogProjectionError, iter_tool_pack_tools
from chatcopilot.tool_packs.catalog import (
    all_tool_bindings,
    get_tool_pack_entry,
    resolve_tool_bindings,
)


def _names(pack: str) -> set[str]:
    return {tool.name for tool in discover_tools(tool_packs=(pack,))}


def test_shared_feishu_module_projects_exact_pack_membership() -> None:
    assert _names("feishu.document") == {
        "feishu_doc_create",
        "feishu_doc_append",
        "feishu_api_get",
    }
    assert _names("feishu.sheet") == {
        "feishu_sheet_read",
        "feishu_sheet_write",
        "feishu_sheet_append",
        "feishu_api_get",
    }
    assert _names("feishu.bitable") == {
        "feishu_bitable_query",
        "feishu_bitable_add",
        "feishu_bitable_update",
        "feishu_api_get",
    }
    assert _names("feishu.wiki") == {
        "feishu_wiki_search",
        "feishu_drive_search",
        "feishu_api_get",
    }
    assert _names("feishu.messaging") == {"feishu_im_send", "feishu_api_get"}

    all_feishu = {
        tool.name
        for tool in discover_tools(
            tool_packs=(
                "feishu.document",
                "feishu.sheet",
                "feishu.bitable",
                "feishu.wiki",
                "feishu.messaging",
            )
        )
    }
    assert len(all_feishu) == 12


def test_builtin_compatibility_mapping_is_derived_from_canonical_bindings() -> None:
    for pack, modules in BUILTIN_TOOL_MODULES_BY_TOOL_PACK.items():
        entry = get_tool_pack_entry(pack)
        assert entry is not None
        assert modules == tuple(
            binding.module
            for binding in entry.tool_bindings
            if binding.module.startswith("chatcopilot.agent.tools.builtin.")
        )

    modules = {binding.module for binding in all_tool_bindings()}
    assert "chatcopilot.agent.tools.builtin.workspace_tools" in modules
    assert "chatcopilot.external_tools.feishu.spec" in modules


def test_component_catalog_and_agent_use_the_same_pack_projection() -> None:
    for pack in (
        "workspace.read_write",
        "memory.chat",
        "persona.manage",
        "mcp.admin",
        "career.intelligence",
        "feishu.sheet",
    ):
        catalog_names = [tool.name for tool in iter_tool_pack_tools(pack)]
        agent_names = [tool.name for tool in discover_tools(tool_packs=(pack,))]
        assert catalog_names == agent_names


def test_binding_resolver_merges_only_identical_module_tool_pairs() -> None:
    bindings = resolve_tool_bindings(("feishu.document", "feishu.sheet"))

    assert len(bindings) == 1
    assert bindings[0].module == "chatcopilot.external_tools.feishu.spec"
    assert bindings[0].tool_names == (
        "feishu_doc_create",
        "feishu_doc_append",
        "feishu_api_get",
        "feishu_sheet_read",
        "feishu_sheet_write",
        "feishu_sheet_append",
    )


def test_component_projection_fails_closed_when_declared_tool_is_missing(monkeypatch) -> None:
    module = ModuleType("chatcopilot.external_tools.tests.missing_projection")
    module.TOOLS = []

    monkeypatch.setattr(
        "chatcopilot.component_catalog.catalog.resolve_tool_bindings",
        lambda _names: (
            type(all_tool_bindings()[0])(
                module=module.__name__,
                tool_names=("missing_tool",),
            ),
        ),
    )

    with pytest.raises(CatalogProjectionError, match="missing_tool"):
        list(iter_tool_pack_tools("tests.missing", module_loader=lambda _path: module))
