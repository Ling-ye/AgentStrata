from __future__ import annotations

import unittest
from types import ModuleType

from chatcopilot.agent.tools.registry import (
    ToolMaterializationError,
    ToolRegistry,
    build_mcp_tools_schema,
    build_tools_schema,
    discover_tools,
)
from chatcopilot.botspec.registry import all_tool_modules, resolve_tool_modules
from chatcopilot.component_catalog import iter_tool_packs
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.tool_packs.catalog import BUILTIN_TOOL_FEATURES, BUILTIN_TOOL_PACKS


def _runtime_tool(name: str = "web_search") -> ToolDef:
    def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary="ok", data={"result": "ok"})

    return ToolDef(
        name=name,
        summary="Runtime provider test tool.",
        input_schema=object_schema(
            {"query": {"type": "string"}},
            required=("query",),
        ),
        output_schema=object_schema(
            {"result": {"type": "string"}},
            required=("result",),
        ),
        handler=handler,
        category="tests.runtime",
        owner="tests",
        module=__name__,
        artifact_kinds=(),
    )


class ExternalToolsRegistryTests(unittest.TestCase):
    def test_public_tool_catalog_views_are_read_only(self) -> None:
        with self.assertRaises(TypeError):
            BUILTIN_TOOL_PACKS["memory.chat"] = BUILTIN_TOOL_PACKS[  # type: ignore[index]
                "memory.chat"
            ]
        with self.assertRaises(TypeError):
            BUILTIN_TOOL_FEATURES["chat.file_uploads"] = BUILTIN_TOOL_FEATURES[  # type: ignore[index]
                "chat.file_uploads"
            ]

    def test_catalog_indexes_provider_modules_without_tool_names(self) -> None:
        for _name, entry in iter_tool_packs():
            self.assertFalse(hasattr(entry, "tool_bindings"))
            self.assertFalse(hasattr(entry, "tool_names"))
            if not entry.dynamic:
                self.assertTrue(entry.provider_module)

        pack_names = tuple(name for name, _entry in iter_tool_packs())
        self.assertEqual(
            set(all_tool_modules()),
            set(resolve_tool_modules(pack_names)),
        )

    def test_empty_tool_pack_selection_has_no_tools(self) -> None:
        self.assertEqual(discover_tools(tool_packs=()), [])

    def test_builtin_tools_are_selected_by_tool_pack(self) -> None:
        memory_names = {tool.name for tool in discover_tools(tool_packs=("memory.chat",))}
        self.assertEqual(memory_names, {"read_memory", "append_memory", "clear_memory"})

        workspace_names = {
            tool.name for tool in discover_tools(tool_packs=("workspace.read_write",))
        }
        self.assertIn("list_workspace", workspace_names)
        self.assertIn("owner_list_workspaces", workspace_names)
        self.assertNotIn("read_memory", workspace_names)
        self.assertNotIn("read_bot_skill", workspace_names)

        code_task_names = {
            tool.name for tool in discover_tools(tool_packs=("dev.code_tasks",))
        }
        self.assertIn("start_code_task", code_task_names)
        self.assertIn("prepare_adapter_source", code_task_names)
        self.assertIn("approve_adapter_source", code_task_names)

    def test_schema_builders_share_the_same_snapshot_names(self) -> None:
        openai_schema, openai_index = build_tools_schema()
        mcp_schema, mcp_index = build_mcp_tools_schema()

        openai_names = {entry["function"]["name"] for entry in openai_schema}
        mcp_names = {entry["name"] for entry in mcp_schema}

        self.assertEqual(openai_names, set(openai_index))
        self.assertEqual(mcp_names, set(mcp_index))
        self.assertEqual(openai_names, mcp_names)
        self.assertEqual(
            [entry["function"]["name"] for entry in openai_schema],
            sorted(openai_names),
        )
        self.assertEqual([entry["name"] for entry in mcp_schema], sorted(mcp_names))
        self.assertTrue(all("outputSchema" in entry for entry in mcp_schema))

    def test_runtime_provider_uses_the_same_registration_path(self) -> None:
        injected = _runtime_tool()
        provider = ToolProvider(
            id="runtime-mcp",
            packs={"mcp.dynamic": (injected,)},
            module=__name__,
        )

        tools = discover_tools(
            tool_packs=("workspace.read_write", "mcp.dynamic"),
            providers=(provider,),
        )

        self.assertIn("web_search", {tool.name for tool in tools})

    def test_unknown_explicit_pack_fails_closed(self) -> None:
        with self.assertRaisesRegex(ToolMaterializationError, "unknown_tool_pack"):
            discover_tools(tool_packs=("missing.pack",))

    def test_enabled_pack_import_failure_contains_provider_evidence(self) -> None:
        def fail(_module_path: str) -> ModuleType:
            raise ImportError("dependency missing")

        with self.assertRaises(ToolMaterializationError) as caught:
            ToolRegistry.from_catalog(("memory.chat",), module_loader=fail)

        self.assertEqual(caught.exception.reason, "import_error:ImportError")
        self.assertEqual(
            caught.exception.module,
            "chatcopilot.agent.tools.builtin.memory_tools",
        )

    def test_enabled_pack_requires_explicit_provider_export(self) -> None:
        module = ModuleType("chatcopilot.agent.tools.builtin.memory_tools")

        with self.assertRaises(ToolMaterializationError) as caught:
            ToolRegistry.from_catalog(
                ("memory.chat",),
                module_loader=lambda _module_path: module,
            )

        self.assertEqual(caught.exception.reason, "missing_or_invalid_tool_provider_export")

    def test_enabled_pack_must_be_owned_by_exported_provider(self) -> None:
        module = ModuleType("chatcopilot.agent.tools.builtin.memory_tools")
        module.TOOL_PROVIDER = ToolProvider(
            id="wrong-pack",
            packs={"other.pack": (_runtime_tool(),)},
            module=module.__name__,
        )

        with self.assertRaises(ToolMaterializationError) as caught:
            ToolRegistry.from_catalog(
                ("memory.chat",),
                module_loader=lambda _module_path: module,
            )

        self.assertEqual(caught.exception.reason, "provider_pack_missing")


if __name__ == "__main__":
    unittest.main()
