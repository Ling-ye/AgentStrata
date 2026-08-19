from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.subagents.registry import (
    SearchCircuitBreaker,
    _make_delegate_tool,
    _with_current_date,
    _with_web_fallback,
)
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.agent.subagents.runner import (
    SubagentRunResult,
    SubagentRuntimeConfig,
    _extract_mcp_error_code,
)
from chatcopilot.agent.subagents.spec import SubagentDef
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.trace import TraceContext, reset_trace, set_trace
from chatcopilot.external_tools.shared.tool_spec import ToolDef


class _FakeLLM:
    model = "fake"

    def chat(self, **_kwargs):
        return ChatResult(content="done")


def _delegate(name: str, payloads: list[dict], calls: list[str]) -> ToolDef:
    def handler(_args: dict):
        calls.append(name)
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return json.dumps(payload, ensure_ascii=False), [], None

    return ToolDef(
        name=name,
        summary=name,
        properties={"objective": {"type": "string"}},
        required=["objective"],
        handler=handler,
    )


class CurrentDatePromptTests(unittest.TestCase):
    def test_runtime_prompt_renderer_preserves_date_after_baseline_refresh(self) -> None:
        runtime = AgentRuntime(
            llm=_FakeLLM(),
            tools=(),
            tools_schema=(),
            runtime_config=ChatConfig(),
        )
        with patch("chatcopilot.agent.context.prompt_builder.date") as mocked_date:
            mocked_date.today.return_value = date(2026, 6, 22)
            session = runtime.new_session(session_id="sid", system_baseline="platform-v1")
            first = session.system_baseline
            session.set_system_baseline("platform-v2")
            second = session.system_baseline

        self.assertIn("准确性与抗迎合规则", first)
        self.assertIn("每个事实性断言都要打标签", first)
        self.assertIn("今天是 2026-06-22", first)
        self.assertIn("platform-v2", second)
        self.assertIn("准确性与抗迎合规则", second)
        self.assertIn("今天是 2026-06-22", second)
        self.assertEqual(second, session.snapshot_messages()[0]["content"])

    def test_same_date_is_stable_and_next_date_only_changes_tail(self) -> None:
        runtime = AgentRuntime(
            llm=_FakeLLM(), tools=(), tools_schema=(), runtime_config=ChatConfig()
        )
        with patch("chatcopilot.agent.context.prompt_builder.date") as mocked_date:
            mocked_date.today.return_value = date(2026, 6, 22)
            session = runtime.new_session(session_id="sid", system_baseline="stable")
            same_day = session.system_baseline
            session.set_system_baseline("stable")
            self.assertEqual(same_day, session.system_baseline)
            mocked_date.today.return_value = date(2026, 6, 23)
            session.set_system_baseline("stable")

        self.assertEqual(
            same_day.removesuffix("2026-06-22。"),
            session.system_baseline.removesuffix("2026-06-23。"),
        )

    def test_runtime_refresh_replaces_persona_and_memory_without_duplication(self) -> None:
        runtime = AgentRuntime(
            llm=_FakeLLM(), tools=(), tools_schema=(), runtime_config=ChatConfig()
        )
        session = runtime.new_session(
            session_id="sid-dynamic",
            system_baseline="stable",
            session_dynamic_tail="old persona",
            memory_snippet_override="old memory",
        )

        session.set_system_context(
            "stable-v2",
            session_dynamic_tail="new persona",
            memory_snippet="new memory",
        )
        rendered = session.system_baseline

        self.assertIn("stable-v2", rendered)
        self.assertIn("new persona", rendered)
        self.assertIn("new memory", rendered)
        self.assertNotIn("old persona", rendered)
        self.assertNotIn("old memory", rendered)
        self.assertEqual(rendered.count("new persona"), 1)
        self.assertEqual(rendered.count("new memory"), 1)


class SearchFallbackTests(unittest.TestCase):
    def test_search_cache_hint_changes_across_dates(self) -> None:
        task = TaskPack(objective="latest")
        with patch("chatcopilot.agent.subagents.registry.date") as mocked_date:
            mocked_date.today.return_value = date(2026, 6, 22)
            first = _with_current_date(task)
            mocked_date.today.return_value = date(2026, 6, 23)
            second = _with_current_date(task)

        self.assertNotEqual(first.cache_key_hint, second.cache_key_hint)

    def test_quota_failure_falls_back_and_skips_primary_until_ttl(self) -> None:
        now = [100.0]
        circuit = SearchCircuitBreaker(clock=lambda: now[0])
        primary_calls: list[str] = []
        fallback_calls: list[str] = []
        primary = _delegate(
            "search_tavily",
            [
                {"ok": False, "error_code": "mcp_quota_exceeded", "summary": "quota"},
                {"ok": True, "summary": "restored"},
            ],
            primary_calls,
        )
        fallback = _delegate(
            "search_searxng", [{"ok": True, "summary": "fresh"}], fallback_calls
        )
        routed = _with_web_fallback(primary=primary, fallback=fallback, circuit=circuit)

        first = json.loads(routed.handler({"objective": "latest"})[0])
        second = json.loads(routed.handler({"objective": "latest again"})[0])
        now[0] += 3601
        third = json.loads(routed.handler({"objective": "probe"})[0])

        self.assertEqual(first["fallback"]["source"], "searxng")
        self.assertEqual(second["fallback"]["source"], "searxng")
        self.assertEqual(primary_calls, ["search_tavily", "search_tavily"])
        self.assertEqual(fallback_calls, ["search_searxng", "search_searxng"])
        self.assertEqual(third["summary"], "restored")

    def test_transient_failure_breaker_expires_after_two_minutes(self) -> None:
        now = [10.0]
        circuit = SearchCircuitBreaker(clock=lambda: now[0])
        circuit.record_failure("searxng", "mcp_timeout")
        self.assertEqual(circuit.blocked("searxng"), "mcp_timeout")
        now[0] += 121
        self.assertIsNone(circuit.blocked("searxng"))

    def test_double_failure_requires_unverified_stale_knowledge_label(self) -> None:
        primary = _delegate(
            "search_tavily",
            [{"ok": False, "error_code": "mcp_unavailable", "summary": "down"}],
            [],
        )
        fallback = _delegate(
            "search_searxng",
            [{"ok": False, "error_code": "mcp_timeout", "summary": "slow"}],
            [],
        )
        routed = _with_web_fallback(
            primary=primary, fallback=fallback, circuit=SearchCircuitBreaker()
        )

        payload = json.loads(routed.handler({"objective": "latest"})[0])

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["limits"]["allow_stale_knowledge"])
        self.assertIn("未联网核实", payload["summary"])

    def test_nested_mcp_error_code_is_extracted(self) -> None:
        nested = {
            "ok": True,
            "summary": json.dumps(
                {"ok": False, "error_code": "mcp_unavailable"}, ensure_ascii=False
            ),
        }
        session = AgentSession(
            session_id="sub",
            llm=_FakeLLM(),
            executor=ToolExecutor(tools=[]),
            tools_schema=[],
            system_baseline="system",
        )
        session._messages.append(
            {"role": "tool", "content": json.dumps(nested, ensure_ascii=False)}
        )
        self.assertEqual(_extract_mcp_error_code(session), "mcp_unavailable")


class SearchDelegateTurnTests(unittest.TestCase):
    def test_failure_throttle_resets_on_new_trace_and_search_gets_current_date(self) -> None:
        seen_tasks = []

        class Runner:
            def run(self, **kwargs):
                seen_tasks.append(kwargs["task"])
                return SubagentRunResult(ok=False, summary="failed")

        tool = _make_delegate_tool(
            "sid",
            SubagentDef(
                name="search_test",
                tool_name="search_test",
                summary="test",
                system_prompt="test",
                kind="search",
            ),
            Runner(),
            SubagentRuntimeConfig(None, 1, 1, 10, 1000),
            lambda _tool: True,
        )

        with patch("chatcopilot.agent.subagents.registry.date") as mocked_date:
            mocked_date.today.return_value = date(2026, 6, 22)
            token = set_trace(TraceContext("turn-1", "span-1", 0))
            try:
                tool.handler(_search_args("latest"))
                tool.handler(_search_args("latest"))
            finally:
                reset_trace(token)
            token = set_trace(TraceContext("turn-2", "span-2", 0))
            try:
                tool.handler(_search_args("latest"))
            finally:
                reset_trace(token)

        self.assertEqual(len(seen_tasks), 3)
        self.assertIn("date:2026-06-22", seen_tasks[-1].cache_key_hint)
        self.assertTrue(any("2026-06-22" in item for item in seen_tasks[-1].constraints))


def _search_args(objective: str) -> dict:
    return {
        "objective": objective,
        "domain": "general",
        "target_sites": [],
        "time_window": "latest as of 2026-06-22",
        "required_fields": ["title", "url", "published_at"],
        "cross_check": False,
    }


if __name__ == "__main__":
    unittest.main()
