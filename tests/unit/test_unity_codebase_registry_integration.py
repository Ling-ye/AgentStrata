from __future__ import annotations

import unittest

from chatcopilot.agent.tools.registry import build_tools_schema
from chatcopilot.botspec.registry import (
    get_tool_pack_entry,
    known_tool_pack_names,
    load_tool_pack_prompt,
    resolve_tool_modules,
)


class UnityCodebaseRegistryIntegrationTests(unittest.TestCase):
    """Smoke-tests that the three tool packs are wired end-to-end."""

    def test_new_tool_packs_are_registered(self) -> None:
        names = known_tool_pack_names()
        self.assertIn("filesystem.windows.read", names)
        self.assertIn("unity.codebase.read", names)
        self.assertIn("unity.skills", names)

    def test_tool_pack_entries_point_to_real_tool_modules(self) -> None:
        cases = {
            "filesystem.windows.read": "chatcopilot.external_tools.windows_fs.tools",
            "unity.codebase.read": "chatcopilot.external_tools.unity_codebase.read_tools",
            "unity.skills": "chatcopilot.external_tools.unity_codebase.skill_tools",
        }
        for pack_name, expected_module in cases.items():
            entry = get_tool_pack_entry(pack_name)
            self.assertIsNotNone(entry, f"missing tool pack entry: {pack_name}")
            self.assertIn(expected_module, entry.tool_modules)

    def test_resolve_tool_modules_for_new_tool_packs(self) -> None:
        resolved = resolve_tool_modules(
            ("filesystem.windows.read", "unity.codebase.read", "unity.skills")
        )
        self.assertEqual(
            resolved,
            (
                "chatcopilot.external_tools.windows_fs.tools",
                "chatcopilot.external_tools.unity_codebase.read_tools",
                "chatcopilot.external_tools.unity_codebase.skill_tools",
            ),
        )

    def test_tool_packs_carry_prompt_fragments(self) -> None:
        for pack_name in ("filesystem.windows.read", "unity.codebase.read", "unity.skills"):
            pack = load_tool_pack_prompt(pack_name)
            self.assertIsNotNone(pack, f"tool pack missing manifest: {pack_name}")
            self.assertEqual(pack.name, pack_name)
            self.assertTrue(pack.prompt_fragments, f"empty prompt_fragments for {pack_name}")

    def test_tools_appear_in_openai_schema_when_tool_packs_enabled(self) -> None:
        schema, index = build_tools_schema(
            tool_packs=("filesystem.windows.read", "unity.codebase.read", "unity.skills")
        )
        names = {entry["function"]["name"] for entry in schema}
        for expected in (
            "win_read_file",
            "win_grep",
            "win_glob",
            "unity_project_read",
            "unity_project_search",
            "unity_project_glob",
            "unity_find_csharp_symbol",
            "unity_path_book",
        ):
            self.assertIn(expected, names, f"tool not exposed: {expected}")
            self.assertIn(expected, index)


if __name__ == "__main__":
    unittest.main()
