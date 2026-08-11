from __future__ import annotations

import unittest

from chatcopilot.botspec.registry import all_tool_modules, resolve_tool_modules
from chatcopilot.agent.tools.registry import build_mcp_tools_schema, build_tools_schema, discover_tools
from chatcopilot.component_catalog import iter_tool_packs
from chatcopilot.external_tools.shared.tool_spec import ToolDef


class ExternalToolsRegistryTests(unittest.TestCase):
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

        workspace_names = {tool.name for tool in discover_tools(tool_packs=("workspace.read_write",))}
        self.assertIn("list_workspace", workspace_names)
        self.assertIn("owner_list_workspaces", workspace_names)
        self.assertNotIn("read_memory", workspace_names)
        self.assertNotIn("read_bot_skill", workspace_names)
        self.assertNotIn("read_feishu_doc", workspace_names)

        code_task_names = {
            tool.name for tool in discover_tools(tool_packs=("dev.code_tasks",))
        }
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

if __name__ == "__main__":
    unittest.main()
