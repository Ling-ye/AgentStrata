from __future__ import annotations

from types import ModuleType

from chatcopilot.agent.tools.registry import ToolRegistry, discover_tools
from chatcopilot.component_catalog import iter_tool_pack_tools
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.tool_packs.catalog import (
    get_tool_pack_entry,
    resolve_tool_modules,
)


def _names(pack: str) -> set[str]:
    return {tool.name for tool in discover_tools(tool_packs=(pack,))}


def _tool(name: str) -> ToolDef:
    def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={})

    return ToolDef(
        name=name,
        summary="Projection test tool.",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler,
        category="tests.projection",
        owner="tests",
        module=__name__,
        artifact_kinds=(),
    )


def test_shared_feishu_provider_projects_exact_pack_membership() -> None:
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


def test_catalog_resolves_only_provider_modules() -> None:
    assert resolve_tool_modules(("feishu.document", "feishu.sheet")) == (
        "chatcopilot.external_tools.feishu",
    )
    assert resolve_tool_modules(("dev.files", "dev.shell", "dev.code_tasks")) == (
        "chatcopilot.external_tools.dev",
    )

    memory = get_tool_pack_entry("memory.chat")
    assert memory is not None
    assert memory.provider_module == "chatcopilot.agent.tools.builtin.memory_tools"
    assert not hasattr(memory, "tool_bindings")
    assert not hasattr(memory, "tool_names")


def test_component_catalog_and_agent_use_the_same_registry_projection() -> None:
    for pack in (
        "workspace.read_write",
        "memory.chat",
        "mcp.admin",
        "career.intelligence",
        "feishu.sheet",
    ):
        catalog_names = [tool.name for tool in iter_tool_pack_tools(pack)]
        agent_names = [tool.name for tool in discover_tools(tool_packs=(pack,))]
        assert catalog_names == agent_names


def test_shared_provider_module_is_loaded_once_for_multiple_selected_packs() -> None:
    module = ModuleType("chatcopilot.external_tools.feishu")
    shared = _tool("shared")
    module.TOOL_PROVIDER = ToolProvider(
        id="feishu",
        packs={
            "feishu.document": (_tool("document"), shared),
            "feishu.sheet": (_tool("sheet"), shared),
        },
        module=module.__name__,
    )
    calls: list[str] = []

    def load(module_path: str) -> ModuleType:
        calls.append(module_path)
        assert module_path == module.__name__
        return module

    registry = ToolRegistry.from_catalog(
        ("feishu.document", "feishu.sheet"),
        module_loader=load,
    )
    snapshot = registry.snapshot(tool_packs=("feishu.document", "feishu.sheet"))

    assert calls == [module.__name__]
    assert tuple(snapshot.index) == ("document", "shared", "sheet")
