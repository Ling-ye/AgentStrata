from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatcopilot.agent.subagents.delegate_tools import has_write_selector
from chatcopilot.agent.subagents.spec import SubagentDef, ToolMatchRule, ToolSelectorSpec
from chatcopilot.contracts.development import (
    DevelopmentTaskScope,
    current_development_task_scope,
    development_task_scope,
    parse_write_scope,
)
from chatcopilot.external_tools.dev.config import DevConfig
from chatcopilot.external_tools.dev.path_guard import DevPathAccessError, ensure_writable
from chatcopilot.external_tools.dev.command_policy import command_argv


class DevelopmentTaskScopeTests(unittest.TestCase):
    def test_parse_write_scope_normalizes_relative_patterns(self) -> None:
        self.assertEqual(
            parse_write_scope("./src/chatcopilot, tests/unit\nspecs/example"),
            ("src/chatcopilot", "tests/unit", "specs/example"),
        )
        self.assertEqual(parse_write_scope("read-only"), ())
        self.assertEqual(parse_write_scope("mcp:jira/issues/123"), ())

    def test_parse_write_scope_rejects_escape(self) -> None:
        with self.assertRaises(ValueError):
            parse_write_scope("../outside")
        with self.assertRaises(ValueError):
            parse_write_scope("/tmp/outside")

    def test_delegated_scope_intersects_global_write_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = DevConfig(repo_root=Path(tmp), allowed_paths=("**",), denied_paths=())
            scope = DevelopmentTaskScope(
                allowed_paths=("src/chatcopilot",),
                task_label="developer",
            )
            with development_task_scope(scope):
                _, normalized = ensure_writable(config, "src/chatcopilot/new.py")
                self.assertEqual(normalized, "src/chatcopilot/new.py")
                with self.assertRaisesRegex(DevPathAccessError, "delegated write_scope"):
                    ensure_writable(config, "tests/unit/test_new.py")

    def test_empty_delegated_scope_denies_file_mutation_and_resets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = DevConfig(repo_root=Path(tmp), allowed_paths=("**",), denied_paths=())
            with development_task_scope(DevelopmentTaskScope()):
                self.assertIsNotNone(current_development_task_scope())
                with self.assertRaises(DevPathAccessError):
                    ensure_writable(config, "new.py")
            self.assertIsNone(current_development_task_scope())
            ensure_writable(config, "new.py")

    def test_validation_uses_argv_and_rejects_unselected_command_families(self) -> None:
        for command in ("python -m pytest tests/unit -q", "git diff --check", "python scripts/check_sdd_specs.py"):
            self.assertTrue(command_argv(command, validation=True))
        for command in ("pip install x", "git commit -m x", "pytest && rm x"):
            with self.assertRaises(ValueError):
                command_argv(command, validation=True)
        self.assertEqual(command_argv('rg "foo;bar" .', validation=True), ["rg", "foo;bar", "."])
        self.assertEqual(command_argv('rg "$(touch unsafe)" .', validation=True)[1], "$(touch unsafe)")

    def test_write_selector_detects_dev_and_mcp_write_rules(self) -> None:
        dev = SubagentDef(
            name="dev",
            tool_name="delegate_dev",
            summary="dev",
            role_prompt="dev",
            selector=ToolSelectorSpec(
                any=(ToolMatchRule(category_prefixes=("dev.",)),)
            ),
        )
        mcp = SubagentDef(
            name="writer",
            tool_name="delegate_writer",
            summary="writer",
            role_prompt="writer",
            selector=ToolSelectorSpec(
                any=(ToolMatchRule(categories=("mcp",), mcp_risk=("write",)),)
            ),
        )
        readonly = SubagentDef(
            name="reader",
            tool_name="delegate_reader",
            summary="reader",
            role_prompt="reader",
            selector=ToolSelectorSpec(
                any=(ToolMatchRule(categories=("mcp",), mcp_risk=("readonly",)),)
            ),
        )

        self.assertTrue(has_write_selector(dev))
        self.assertTrue(has_write_selector(mcp))
        self.assertFalse(has_write_selector(readonly))


if __name__ == "__main__":
    unittest.main()
