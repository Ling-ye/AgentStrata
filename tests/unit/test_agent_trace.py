from __future__ import annotations

import json
import unittest

from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.protocol import (
    AgentTask,
    LlmCallStarted,
    LlmCallFinished,
    SpanFinished,
    SpanStarted,
    ToolFinished,
    ToolStarted,
)
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.subagents.registry import build_subagent_tools
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import SubagentBudgetSpec, SubagentSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef, build_openai_schema


class _ScriptedLLM:
    def __init__(self, results: list[ChatResult]) -> None:
        self._results = results
        self._idx = 0

    def chat(self, **kwargs):
        result = self._results[min(self._idx, len(self._results) - 1)]
        self._idx += 1
        return result


def _tool(name: str, *, category: str = "") -> ToolDef:
    return ToolDef(
        name=name,
        summary=f"{name} summary",
        properties={},
        required=[],
        handler=lambda args: (f"{name} ok", [], None),
        category=category,
    )


def _call(name: str, args: dict, call_id: str = "c1") -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
        finish_reason="tool_calls",
    )


class AgentTraceTests(unittest.TestCase):
    def test_main_tool_spans_are_stamped(self) -> None:
        ping = _tool("ping", category="agent")
        llm = _ScriptedLLM([_call("ping", {}), ChatResult(content="完成")])
        session = AgentSession(
            session_id="sid",
            llm=llm,
            executor=ToolExecutor(tools=[ping]),
            tools_schema=[build_openai_schema(ping)],
            system_baseline="baseline",
        )
        events = []
        session.run_task(AgentTask(text="go", metadata={"trace_id": "trace_fixed"}), on_event=events.append)

        started = [e for e in events if isinstance(e, ToolStarted)]
        finished = [e for e in events if isinstance(e, ToolFinished)]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].trace_id, "trace_fixed")
        self.assertEqual(started[0].depth, 0)
        self.assertIsNotNone(started[0].span_id)
        self.assertEqual(started[0].parent_span_id, finished[0].parent_span_id)
        llm_calls = [e for e in events if isinstance(e, LlmCallFinished)]
        llm_starts = [e for e in events if isinstance(e, LlmCallStarted)]
        self.assertEqual(len(llm_starts), len(llm_calls))
        self.assertEqual(llm_starts[0].span_id, llm_calls[0].span_id)
        self.assertGreater(llm_calls[0].input_message_count, 0)
        self.assertGreater(llm_calls[0].input_estimated_tokens, 0)
        self.assertGreater(llm_calls[0].tool_schema_estimated_tokens, 0)
        self.assertGreaterEqual(llm_calls[0].tool_schema_count, 1)
        self.assertEqual(started[0].span_id, finished[0].span_id)

    def test_subagent_spans_bubble_onto_main_trace(self) -> None:
        csv_tool = _tool("read_file", category="dev.files")
        subagent_llm = _ScriptedLLM(
            [
                _call("read_file", {}, "call_csv"),
                _call("submit_result", {"summary": "趋势平稳"}, "call_submit"),
                ChatResult(content="done"),
            ]
        )
        delegate_tools = build_subagent_tools(
            session_id="sid",
            subagents=SubagentSpec(
                include=("developer",),
                agents={"developer": SubagentBudgetSpec(max_model_turns=3, max_tool_calls=3)},
            ),
            main_llm=subagent_llm,
            main_config=ChatConfig(),
            base_tools=(csv_tool,),
        )
        delegate = delegate_tools[0]

        main_llm = _ScriptedLLM(
            [
                _call(
                    delegate.name,
                    {"task": "看趋势", "write_scope": ["tests"]},
                    "call_deleg",
                ),
                ChatResult(content="已总结"),
            ]
        )
        session = AgentSession(
            session_id="sid",
            llm=main_llm,
            executor=ToolExecutor(tools=[delegate]),
            tools_schema=[build_openai_schema(delegate)],
            system_baseline="baseline",
        )
        events = []
        session.run_task(AgentTask(text="go", metadata={"trace_id": "T"}), on_event=events.append)

        span_started = [e for e in events if isinstance(e, SpanStarted)]
        span_finished = [e for e in events if isinstance(e, SpanFinished)]
        self.assertEqual(len(span_started), 1)
        self.assertEqual(span_started[0].kind, "subagent")
        self.assertEqual(span_started[0].depth, 1)

        # 主 delegate 工具 span = depth 0；subagent 内部 read_file span = depth 2。
        tool_started = [e for e in events if isinstance(e, ToolStarted)]
        depths = {e.name: e.depth for e in tool_started}
        self.assertEqual(depths.get(delegate.name), 0)
        self.assertEqual(depths.get("read_file"), 2)

        # SpanFinished 携带 transcript 与结构化结果，供 recorder 落盘。
        self.assertTrue(span_finished[0].ok)
        self.assertIn("transcript", span_finished[0].data)
        self.assertEqual(span_finished[0].data["result"]["summary"], "趋势平稳")
        # 所有事件共享同一 trace_id。
        self.assertTrue(all(e.trace_id == "T" for e in tool_started))


if __name__ == "__main__":
    unittest.main()
