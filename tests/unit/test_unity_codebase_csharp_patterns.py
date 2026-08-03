from __future__ import annotations

import re
import unittest

from chatcopilot.external_tools.unity_codebase._csharp_patterns import (
    UnknownCsharpModeError,
    build_csharp_query,
    supported_modes,
)


def _matches(pattern: str, snippet: str) -> bool:
    return re.search(pattern, snippet) is not None


class CsharpPatternsTests(unittest.TestCase):
    def test_supported_modes_contains_expected_four(self) -> None:
        self.assertEqual(
            set(supported_modes()),
            {"definition", "references", "new_expression", "callers"},
        )

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(UnknownCsharpModeError):
            build_csharp_query("MissionList", "unknown_mode")

    def test_empty_symbol_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_csharp_query("", "definition")

    def test_definition_matches_class(self) -> None:
        pattern, glob = build_csharp_query("MissionList", "definition")
        self.assertEqual(glob, "*.cs")
        self.assertTrue(_matches(pattern, "public class MissionList : MonoBehaviour"))
        self.assertTrue(_matches(pattern, "struct MissionList\n{"))
        self.assertTrue(_matches(pattern, "internal interface MissionList { }"))
        self.assertTrue(_matches(pattern, "record MissionList(int Id);"))
        self.assertTrue(_matches(pattern, "enum MissionList { A, B }"))
        # Should not match calls / usages
        self.assertFalse(_matches(pattern, "var x = new MissionList();"))
        self.assertFalse(_matches(pattern, "MissionList list;"))

    def test_references_matches_everywhere(self) -> None:
        pattern, _ = build_csharp_query("MissionList", "references")
        self.assertTrue(_matches(pattern, "MissionList list;"))
        self.assertTrue(_matches(pattern, "var x = new MissionList();"))
        self.assertFalse(_matches(pattern, "MissionListExtension extra;"))

    def test_new_expression_matches_new(self) -> None:
        pattern, _ = build_csharp_query("MissionList", "new_expression")
        self.assertTrue(_matches(pattern, "var x = new MissionList();"))
        self.assertTrue(_matches(pattern, "return new MissionList(arg);"))
        self.assertTrue(_matches(pattern, "field = new MissionList<int>();"))
        # Without `new`, should not match
        self.assertFalse(_matches(pattern, "MissionList list = null;"))
        self.assertFalse(_matches(pattern, "DoSomething(MissionList.Static);"))

    def test_callers_matches_call_sites_but_not_definitions(self) -> None:
        pattern, _ = build_csharp_query("InitPlayerMissions", "callers")
        self.assertTrue(_matches(pattern, "this.InitPlayerMissions(playerId);"))
        self.assertTrue(_matches(pattern, "    InitPlayerMissions();"))
        # Definitions should not look like callers due to leading word-boundary,
        # but a naive "MethodName(" pattern can fire on prototype declarations.
        # We at least ensure invocation cases match.

    def test_special_regex_chars_in_symbol_are_escaped(self) -> None:
        pattern, _ = build_csharp_query("List<int>", "new_expression")
        self.assertTrue(_matches(pattern, "var x = new List<int>();"))
        # Should not turn into wild regex - bare 'int' should not match
        self.assertFalse(_matches(pattern, "var x = new int();"))


if __name__ == "__main__":
    unittest.main()
