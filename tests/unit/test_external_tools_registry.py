from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from chatcopilot.botspec.registry import all_tool_modules, resolve_tool_modules
from chatcopilot.agent.tools.registry import (
    ToolMaterializationError,
    build_mcp_tools_schema,
    build_tools_schema,
    discover_tools,
)
from chatcopilot.component_catalog import iter_tool_packs
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.tool_packs.catalog import BUILTIN_TOOL_FEATURES, BUILTIN_TOOL_PACKS


class ExternalToolsRegistryTests(unittest.TestCase):
    def test_public_tool_catalog_views_are_read_only(self) -> None:
        with self.assertRaises(TypeError):
            BUILTIN_TOOL_PACKS["memory.chat"] = BUILTIN_TOOL_PACKS["memory.chat"]  # type: ignore[index]
        with self.assertRaises(TypeError):
            BUILTIN_TOOL_FEATURES["chat.file_uploads"] = BUILTIN_TOOL_FEATURES[
                "chat.file_uploads"
            ]  # type: ignore[index]

    def test_default_discovery_uses_tool_pack_modules(self) -> None:
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
        self.assertNotIn("read_feishu_doc", workspace_names)

        code_task_names = {tool.name for tool in discover_tools(tool_packs=("dev.code_tasks",))}
        self.assertIn("start_code_task", code_task_names)
        self.assertIn("prepare_adapter_source", code_task_names)
        self.assertIn("approve_adapter_source", code_task_names)

    def test_schema_builders_share_the_same_tool_names(self) -> None:
        openai_schema, openai_index = build_tools_schema()
        mcp_schema, mcp_index = build_mcp_tools_schema()

        openai_names = {entry["function"]["name"] for entry in openai_schema}
        mcp_names = {entry["name"] for entry in mcp_schema}

        self.assertEqual(openai_names, set(openai_index))
        self.assertEqual(mcp_names, set(mcp_index))
        self.assertEqual(openai_names, mcp_names)
        self.assertNotIn("external_diff", openai_names)
        self.assertNotIn("diff", openai_names)
        self.assertNotIn("datasource_sync", openai_names)
        self.assertEqual(
            [entry["function"]["name"] for entry in openai_schema],
            sorted(openai_names),
        )
        self.assertEqual([entry["name"] for entry in mcp_schema], sorted(mcp_names))

    def test_mcp_tools_can_be_injected_into_discovery(self) -> None:
        injected = ToolDef(
            name="web_search",
            summary="Search through an external MCP server.",
            properties={"query": {"type": "string"}},
            required=["query"],
            handler=lambda _args: ("ok", [], None),
            category="mcp",
        )

        tools = discover_tools(tool_packs=("workspace.read_write",), mcp_tools=(injected,))
        names = {tool.name for tool in tools}

        self.assertIn("web_search", names)

    def test_unknown_explicit_pack_fails_closed(self) -> None:
        with self.assertRaisesRegex(ToolMaterializationError, "unknown_tool_pack"):
            discover_tools(tool_packs=("missing.pack",))

    def test_enabled_pack_import_failure_contains_binding_evidence(self) -> None:
        with patch(
            "chatcopilot.agent.tools.registry.importlib.import_module",
            side_effect=ImportError("dependency missing"),
        ):
            with self.assertRaises(ToolMaterializationError) as caught:
                discover_tools(tool_packs=("memory.chat",))
        self.assertEqual(caught.exception.pack_names, ("memory.chat",))
        self.assertEqual(
            caught.exception.module,
            "chatcopilot.agent.tools.builtin.memory_tools",
        )
        self.assertEqual(caught.exception.reason, "import_error:ImportError")

    def test_enabled_pack_missing_declared_tool_fails_closed(self) -> None:
        exported = ToolDef(
            name="read_memory",
            summary="read",
            properties={},
            required=[],
            handler=lambda _args: ("ok", [], None),
        )
        with patch(
            "chatcopilot.agent.tools.registry.importlib.import_module",
            return_value=SimpleNamespace(TOOLS=[exported]),
        ):
            with self.assertRaises(ToolMaterializationError) as caught:
                discover_tools(tool_packs=("memory.chat",))
        self.assertEqual(caught.exception.reason, "declared_tools_missing")
        self.assertEqual(
            caught.exception.tool_names,
            ("append_memory", "clear_memory"),
        )

    def test_enabled_pack_requires_non_empty_tools_export(self) -> None:
        with patch(
            "chatcopilot.agent.tools.registry.importlib.import_module",
            return_value=SimpleNamespace(TOOLS=[]),
        ):
            with self.assertRaises(ToolMaterializationError) as caught:
                discover_tools(tool_packs=("memory.chat",))
        self.assertEqual(caught.exception.reason, "missing_or_empty_tools_export")

    def test_enabled_pack_rejects_invalid_tool_export(self) -> None:
        with patch(
            "chatcopilot.agent.tools.registry.importlib.import_module",
            return_value=SimpleNamespace(TOOLS=[object()]),
        ):
            with self.assertRaises(ToolMaterializationError) as caught:
                discover_tools(tool_packs=("memory.chat",))
        self.assertEqual(caught.exception.reason, "invalid_tool_export")

    def test_enabled_pack_rejects_duplicate_tool_export(self) -> None:
        exported = ToolDef(
            name="read_memory",
            summary="read",
            properties={},
            required=[],
            handler=lambda _args: ("ok", [], None),
        )
        with patch(
            "chatcopilot.agent.tools.registry.importlib.import_module",
            return_value=SimpleNamespace(TOOLS=[exported, exported]),
        ):
            with self.assertRaises(ToolMaterializationError) as caught:
                discover_tools(tool_packs=("memory.chat",))
        self.assertEqual(caught.exception.reason, "duplicate_tool_export")
        self.assertEqual(caught.exception.tool_names, ("read_memory",))


if __name__ == "__main__":
    unittest.main()
