from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.subagents.cache import build_cache_key
from chatcopilot.agent.subagents.context_pack import ContextPackBuilder
from chatcopilot.agent.subagents.registry import build_subagent_tools
from chatcopilot.agent.subagents.spec import CachePolicySpec, ContextPolicySpec, ToolMatchRule, ToolSelectorSpec
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.loader import _parse_subagents, load_botspec, validate_botspec
from chatcopilot.botspec.model import CustomSubagentSpec, SubagentBudgetSpec, SubagentSpec
from chatcopilot.contracts.tools import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


class _ScriptedLLM:
    def __init__(self, results: list[ChatResult]) -> None:
        self.results = results
        self.index = 0
        self.calls = 0
        self.messages_seen: list[list[dict]] = []
        self.seen_tools: list[str] = []

    def chat(self, **kwargs):
        self.calls += 1
        self.messages_seen.append(kwargs.get("messages") or [])
        self.seen_tools = [
            str((entry.get("function") or {}).get("name") or "")
            for entry in kwargs.get("tools") or []
        ]
        result = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        return result


def _submit(summary: str) -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[
            {
                "id": f"call_{summary}",
                "type": "function",
                "function": {
                    "name": "submit_result",
                    "arguments": json.dumps(
                        {
                            "summary": summary,
                            "findings": [{"text": summary}],
                            "commands_run": [{"tool": "submit_result"}],
                            "confidence": "high",
                            "ok": True,
                        }
                    ),
                },
            }
        ],
        finish_reason="tool_calls",
    )


def _done() -> ChatResult:
    return ChatResult(content="done")


def _tool(name: str, *, category: str = "", owner: str = "", tags: tuple[str, ...] = ()) -> ToolDef:
    def _handler(_args: dict, _ctx: ToolContext) -> ToolResult:
        result = f"{name} done"
        return ToolResult(ok=True, summary=result, data={"result": result})

    tool = ToolDef(
        name=name,
        summary=f"{name} summary",
        input_schema=object_schema(),
        output_schema=object_schema(
            {"result": {"type": "string"}}, required=("result",)
        ),
        handler=_handler,
        category=category,
        owner=owner,
        module=__name__,
        artifact_kinds=(),
    )
    if tags:
        tool.metadata["tags"] = list(tags)
    return tool


class SubagentV2Tests(unittest.TestCase):
    def test_parse_subagent_v2_fields(self) -> None:
        spec = _parse_subagents(
            {
                "include": ["web_research"],
                "workflows": ["research"],
                "max_workflow_depth": 2,
                "web_research": {
                    "max_model_turns": 2,
                    "prompt": {"role": "prompts/subagents/research.md"},
                    "context_policy": {"allowed_task_fields": ["objective", "resources"]},
                    "cache_policy": {"enabled": True, "ttl_seconds": 30, "namespace": "test"},
                },
            }
        )

        self.assertEqual(spec.workflows, ("research",))
        self.assertEqual(spec.max_workflow_depth, 2)
        self.assertEqual(spec.agents["web_research"].max_model_turns, 2)
        self.assertEqual(
            spec.overrides["web_research"].role_prompt_path,
            "prompts/subagents/research.md",
        )
        self.assertEqual(spec.overrides["web_research"].context_policy.allowed_task_fields, ("objective", "resources"))
        self.assertEqual(spec.overrides["web_research"].cache_policy.namespace, "test")
        self.assertIn("prompt", spec.overrides["web_research"].override_fields)
        self.assertIn("cache_policy", spec.overrides["web_research"].override_fields)
        self.assertNotIn("selector", spec.overrides["web_research"].override_fields)

    def test_context_and_cache_policies_reject_invalid_booleans(self) -> None:
        invalid_fields = (
            ("context_policy", "include_tool_summary"),
            ("context_policy", "include_history"),
            ("context_policy", "include_allowed_tools"),
            ("cache_policy", "enabled"),
            ("cache_policy", "include_resource_hashes"),
        )
        for policy, field in invalid_fields:
            for value in ("invalid", None):
                with self.subTest(
                    policy=policy,
                    field=field,
                    value=value,
                ), self.assertRaisesRegex(
                    ValueError,
                    rf"agents\.web_research\.{policy}\.{field}",
                ):
                    _parse_subagents(
                        {
                            "presets": ["web_research"],
                            "web_research": {policy: {field: value}},
                        }
                    )

    def test_custom_policy_error_uses_exact_botspec_path(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"agents\.custom\[0\]\.context_policy\.include_history",
        ):
            _parse_subagents(
                {
                    "custom": [
                        {
                            "name": "custom_reader",
                            "context_policy": {"include_history": None},
                        }
                    ]
                }
            )

    def test_validate_rejects_invalid_workflow_depth(self) -> None:
        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        bad = replace(spec, agents=replace(spec.agents, max_workflow_depth=3))

        messages = "\n".join(issue.message for issue in validate_botspec(bad) if issue.level == "error")

        self.assertIn("max_workflow_depth", messages)

    def test_context_pack_filters_task_fields(self) -> None:
        task = TaskPack(
            objective="inspect repo",
            user_intent="do not leak",
            resources=("src/a.py",),
            excluded_context=("old irrelevant discussion",),
        )
        pack = ContextPackBuilder().build(
            task=task,
            tools=(_tool("read_text_head", category="agent.workspace"),),
            policy=ContextPolicySpec(allowed_task_fields=("objective", "resources")),
        )
        rendered = pack.render()

        self.assertIn("inspect repo", rendered)
        self.assertIn("src/a.py", rendered)
        self.assertNotIn("do not leak", rendered)
        self.assertNotIn("old irrelevant discussion", rendered)

    def test_cache_key_changes_on_resource_and_toolset(self) -> None:
        policy = CachePolicySpec(enabled=True, ttl_seconds=60)
        base_task = TaskPack(objective="research", resources=("a.md",))
        key1 = build_cache_key(
            subagent_name="web_research",
            version="2",
            model="m",
            prompt_fingerprint="p",
            tools=(_tool("web_search", category="mcp", owner="web"),),
            task=base_task,
            policy=policy,
        )
        key2 = build_cache_key(
            subagent_name="web_research",
            version="2",
            model="m",
            prompt_fingerprint="p",
            tools=(_tool("web_search_v2", category="mcp", owner="web"),),
            task=base_task,
            policy=policy,
        )
        key3 = build_cache_key(
            subagent_name="web_research",
            version="2",
            model="m",
            prompt_fingerprint="p",
            tools=(_tool("web_search", category="mcp", owner="web"),),
            task=TaskPack(objective="research", resources=("b.md",)),
            policy=policy,
        )

        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)

    def test_subagent_cache_reuses_result_for_same_task(self) -> None:
        llm = _ScriptedLLM([_submit("cached_result"), _done()])
        custom_sub = CustomSubagentSpec(
            name="cacheable_helper",
            tool_name="delegate_cacheable",
            summary="A cacheable helper for testing",
            selector=ToolSelectorSpec(any=(ToolMatchRule(categories=("dev.files",)),)),
            budget=SubagentBudgetSpec(max_model_turns=2, max_tool_calls=2),
            role_prompt="helper",
            cache_policy=CachePolicySpec(enabled=True, ttl_seconds=300),
        )
        tools = build_subagent_tools(
            session_id="sid-cache-v2",
            subagents=SubagentSpec(custom=(custom_sub,)),
            main_llm=llm,
            main_config=ChatConfig(),
            base_tools=(_tool("read_file", category="dev.files"),),
        )
        executor = ToolExecutor(tools=list(tools))

        args = {"objective": "same task", "write_scope": "read-only"}
        first = executor.execute("delegate_cacheable", args).data
        second = executor.execute("delegate_cacheable", args).data

        self.assertEqual(first["summary"], "cached_result")
        self.assertEqual(second["summary"], "cached_result")
        self.assertEqual(llm.calls, 2)

    def test_write_risk_selector_can_expose_tool_but_user_facing_stays_hidden(self) -> None:
        llm = _ScriptedLLM([_submit("write ok"), _done()])
        writer = _tool("mcp_write_issue", category="mcp", owner="jira")
        writer.metadata.update({"mcp_exposure": "subagent", "mcp_allowed_subagents": ["writer"], "mcp_risk": "write"})
        sender = _tool("send_files_to_user", category="agent.workspace")
        sender.metadata["user_facing"] = True
        custom = SubagentSpec(
            custom=(
                CustomSubagentSpec(
                    name="writer",
                    tool_name="delegate_writer",
                    summary="writer",
                    selector=ToolSelectorSpec(any=(ToolMatchRule(categories=("mcp",), mcp_risk=("write",)),)),
                    budget=SubagentBudgetSpec(max_model_turns=2, max_tool_calls=2),
                    role_prompt="writer",
                ),
            )
        )
        tools = build_subagent_tools(
            session_id="sid-writer-v2",
            subagents=custom,
            main_llm=llm,
            main_config=ChatConfig(),
            base_tools=(writer, sender),
        )

        payload = (
            ToolExecutor(tools=list(tools))
            .execute(
                "delegate_writer",
                {"objective": "write", "write_scope": "mcp:jira/issues"},
            )
            .data
        )

        self.assertTrue(payload["ok"])
        self.assertIn("mcp_write_issue", llm.seen_tools)
        self.assertNotIn("send_files_to_user", llm.seen_tools)

    def test_unknown_workflow_rejected_by_validate(self) -> None:
        from chatcopilot.botspec.loader import validate_botspec, load_botspec

        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        invalid = replace(spec, agents=replace(spec.agents, workflows=("nonexistent_workflow",)))
        issues = validate_botspec(invalid)
        self.assertTrue(any("nonexistent_workflow" in issue.message for issue in issues))

    def test_removed_research_workflow_is_not_exposed(self) -> None:
        tools = build_subagent_tools(
            session_id="sid-no-legacy-research",
            subagents=SubagentSpec(workflows=("research",)),
            main_llm=_ScriptedLLM([_done()]),
            main_config=ChatConfig(),
            base_tools=(),
        )

        self.assertNotIn("run_research_workflow", {tool.name for tool in tools})


if __name__ == "__main__":
    unittest.main()
