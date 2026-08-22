"""Tests for subagent context pack: memory/RAG injection and token budget."""

from __future__ import annotations

import json
import unittest

from chatcopilot.agent.subagents.context_pack import ContextPackBuilder
from chatcopilot.agent.subagents.spec import ContextPolicySpec
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.contracts.tools import ToolDef


def _dummy_tool(name: str) -> ToolDef:
    return ToolDef(
        name=name,
        summary=f"Tool {name}",
        properties={},
        required=[],
        handler=lambda _: ("ok", [], None),
    )


class ContextPackMemoryRagTests(unittest.TestCase):
    def _build(self, *, memory=None, rag=None, policy=None):
        task = TaskPack(objective="test objective")
        tools = [_dummy_tool("tool_a")]
        policy = policy or ContextPolicySpec()
        return ContextPackBuilder().build(
            task=task,
            tools=tools,
            policy=policy,
            memory_summary=memory,
            rag_snippets=rag,
        )

    def test_no_memory_no_rag(self):
        pack = self._build()
        rendered = json.loads(pack.render())
        self.assertNotIn("memory_summary", rendered)
        self.assertNotIn("rag_snippets", rendered)

    def test_memory_injected(self):
        pack = self._build(memory="User prefers dark mode.")
        rendered = json.loads(pack.render())
        self.assertEqual(rendered["memory_summary"], "User prefers dark mode.")

    def test_rag_injected(self):
        pack = self._build(rag=["snippet about game performance"])
        rendered = json.loads(pack.render())
        self.assertEqual(rendered["rag_snippets"], ["snippet about game performance"])

    def test_both_injected(self):
        pack = self._build(
            memory="Preference: concise answers",
            rag=["RAG hit 1", "RAG hit 2"],
        )
        rendered = json.loads(pack.render())
        self.assertEqual(rendered["memory_summary"], "Preference: concise answers")
        self.assertEqual(len(rendered["rag_snippets"]), 2)

    def test_empty_strings_excluded(self):
        pack = self._build(memory="", rag=["", "  "])
        rendered = json.loads(pack.render())
        self.assertNotIn("memory_summary", rendered)
        self.assertNotIn("rag_snippets", rendered)


class ContextPackFieldFilterTests(unittest.TestCase):
    def test_allowed_fields_filtering(self):
        task = TaskPack(
            objective="main goal",
            user_intent="help me",
            write_scope="src/",
            constraints=("no breaking changes",),
        )
        policy = ContextPolicySpec(
            allowed_task_fields=("objective", "constraints"),
        )
        pack = ContextPackBuilder().build(task=task, tools=[], policy=policy)
        rendered = json.loads(pack.render())
        tp = rendered["task_pack"]
        self.assertEqual(tp["objective"], "main goal")
        self.assertEqual(tp["constraints"], ["no breaking changes"])
        self.assertEqual(tp["write_scope"], "")

    def test_search_constraints_survive_context_pack(self):
        task = TaskPack(
            objective="Find official release notes",
            domain="technical",
            target_sites=("unity.com",),
            time_window="past 30 days",
            required_fields=("title", "url", "published_at"),
            cross_check=True,
        )

        pack = ContextPackBuilder().build(
            task=task,
            tools=[],
            policy=ContextPolicySpec(),
        )
        rendered = json.loads(pack.render())["task_pack"]

        self.assertEqual(rendered["domain"], "technical")
        self.assertEqual(rendered["target_sites"], ["unity.com"])
        self.assertEqual(rendered["time_window"], "past 30 days")
        self.assertEqual(rendered["required_fields"], ["title", "url", "published_at"])
        self.assertTrue(rendered["cross_check"])


if __name__ == "__main__":
    unittest.main()
