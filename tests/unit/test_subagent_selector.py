from __future__ import annotations

import unittest

from chatcopilot.agent.subagents.selector import build_predicate, is_user_facing
from chatcopilot.agent.subagents.spec import ToolMatchRule, ToolSelectorSpec
from chatcopilot.contracts.tools import (
    TOOL_AUDIENCE_MAIN,
    TOOL_AUDIENCES,
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


def _tool(name: str, **kwargs) -> ToolDef:
    def handler(_args: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary=name)

    return ToolDef(
        name=name,
        summary=f"{name} summary",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler,
        category=kwargs.get("category", ""),
        owner=kwargs.get("owner", ""),
        module=kwargs.get("module", ""),
        audiences=kwargs.get("audiences", TOOL_AUDIENCES),
        metadata=kwargs.get("metadata", {}),
    )


class SelectorTests(unittest.TestCase):
    def test_empty_selector_matches_nothing(self) -> None:
        pred = build_predicate(ToolSelectorSpec())
        self.assertFalse(pred(_tool("anything", category="analysis.metrics")))

    def test_or_across_rules(self) -> None:
        selector = ToolSelectorSpec(
            any=(
                ToolMatchRule(names=("list_workspace",)),
                ToolMatchRule(category_prefixes=("analysis.",)),
            )
        )
        pred = build_predicate(selector)
        self.assertTrue(pred(_tool("list_workspace", category="agent.workspace")))
        self.assertTrue(pred(_tool("metric_reader", category="analysis.metrics")))
        self.assertFalse(pred(_tool("win_read_file", category="filesystem.windows.read")))

    def test_and_within_rule(self) -> None:
        selector = ToolSelectorSpec(
            any=(ToolMatchRule(categories=("mcp",), mcp_risk=("readonly", "search")),)
        )
        pred = build_predicate(selector)
        readonly_mcp = _tool("gh_search", category="mcp", metadata={"mcp_risk": "readonly"})
        write_mcp = _tool("gh_create", category="mcp", metadata={"mcp_risk": "write"})
        non_mcp_readonly = _tool("read_text_head", category="agent.workspace")
        self.assertTrue(pred(readonly_mcp))
        self.assertFalse(pred(write_mcp))
        self.assertFalse(pred(non_mcp_readonly))

    def test_user_facing_is_always_excluded(self) -> None:
        selector = ToolSelectorSpec(any=(ToolMatchRule(names=("send_files_to_user",)),))
        pred = build_predicate(selector)
        sender = _tool("send_files_to_user", metadata={"user_facing": True})
        self.assertTrue(is_user_facing(sender))
        self.assertFalse(pred(sender))

    def test_main_only_audience_is_always_excluded(self) -> None:
        selector = ToolSelectorSpec(any=(ToolMatchRule(category_prefixes=("agent.",)),))
        pred = build_predicate(selector)

        self.assertTrue(pred(_tool("shared", category="agent.shared")))
        self.assertFalse(
            pred(
                _tool(
                    "main_only",
                    category="agent.control",
                    audiences=(TOOL_AUDIENCE_MAIN,),
                )
            )
        )

    def test_exclude_names_wins(self) -> None:
        selector = ToolSelectorSpec(
            any=(ToolMatchRule(category_prefixes=("analysis.",)),),
            exclude_names=("dangerous_tool",),
        )
        pred = build_predicate(selector)
        self.assertFalse(pred(_tool("dangerous_tool", category="analysis.metrics")))
        self.assertTrue(pred(_tool("safe_tool", category="analysis.metrics")))


if __name__ == "__main__":
    unittest.main()
