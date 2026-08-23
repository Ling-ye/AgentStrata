from __future__ import annotations

from tests.prompt_plan_fixture import prompt_plan

import json
import unittest

from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.contracts.agent import (
    AgentTask,
    ContextSnapshotPrepared,
    LlmCallStarted,
    LlmCallFinished,
    SpanFinished,
    SpanStarted,
    ToolFinished,
    ToolStarted,
)
from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.subagents.registry import build_subagent_tools
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import SubagentBudgetSpec, SubagentSpec
from chatcopilot.contracts.tools import (
    ToolContext,
    ToolDef,
    ToolResult,
    build_openai_schema,
    object_schema,
)


class _ScriptedLLM:
    def __init__(self, results: list[ChatResult]) -> None:
        self._results = results
        self._idx = 0

    def chat(self, **kwargs):
        result = self._results[min(self._idx, len(self._results) - 1)]
        self._idx += 1
        return result


class _FailingLLM:
    model = "failing-model"

    def chat(self, **kwargs):
        del kwargs
        raise RuntimeError("simulated model failure")


def _tool(name: str, *, category: str = "") -> ToolDef:
    def handler(_args: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary=f"{name} ok")

    return ToolDef(
        name=name,
        summary=f"{name} summary",
        input_schema=object_schema(additional_properties=True),
        output_schema=object_schema(),
        handler=handler,
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
    def test_native_and_langgraph_share_context_snapshot_conformance(self) -> None:
        for session_type, backend in (
            (AgentSession, "native"),
            (LangGraphAgentSession, "langgraph"),
        ):
            with self.subTest(backend=backend):
                session = session_type(
                    session_id=f"sid-{backend}",
                    llm=_ScriptedLLM([ChatResult(content="完成")]),
                    executor=ToolExecutor(tools=[]),
                    tools_schema=[],
                    prompt_plan=prompt_plan("baseline"),
                )
                events: list[object] = []

                session.run_task(
                    AgentTask(text="go", metadata={"trace_id": f"trace-{backend}"}),
                    on_event=events.append,
                )

                starts = [event for event in events if isinstance(event, LlmCallStarted)]
                snapshots = {
                    event.snapshot_id: event
                    for event in events
                    if isinstance(event, ContextSnapshotPrepared)
                }
                self.assertGreater(len(starts), 0)
                self.assertEqual(len(starts), len(snapshots))
                for started in starts:
                    self.assertIn(started.context_snapshot_id, snapshots)
                    snapshot = snapshots[started.context_snapshot_id]
                    self.assertEqual(snapshot.backend, backend)
                    self.assertEqual(snapshot.span_id, started.span_id)
                self.assertEqual(snapshot.trace_id, started.trace_id)
                self.assertEqual(started.backend, backend)

    def test_native_and_langgraph_close_failed_llm_calls_with_snapshot_correlation(
        self,
    ) -> None:
        for session_type, backend in (
            (AgentSession, "native"),
            (LangGraphAgentSession, "langgraph"),
        ):
            with self.subTest(backend=backend):
                session = session_type(
                    session_id=f"sid-failed-{backend}",
                    llm=_FailingLLM(),
                    executor=ToolExecutor(tools=[]),
                    tools_schema=[],
                    prompt_plan=prompt_plan("baseline"),
                )
                events: list[object] = []

                result = session.run_task(
                    AgentTask(text="go", metadata={"trace_id": f"trace-failed-{backend}"}),
                    on_event=events.append,
                )

                snapshots = [
                    event for event in events if isinstance(event, ContextSnapshotPrepared)
                ]
                starts = [event for event in events if isinstance(event, LlmCallStarted)]
                finishes = [event for event in events if isinstance(event, LlmCallFinished)]
                self.assertEqual(result.stop_reason, "llm_error")
                self.assertEqual(len(snapshots), 1)
                self.assertEqual(len(starts), 1)
                self.assertEqual(len(finishes), 1)
                self.assertFalse(finishes[0].ok)
                self.assertEqual(finishes[0].finish_reason, "failed")
                self.assertEqual(finishes[0].backend, backend)
                self.assertEqual(finishes[0].trace_id, starts[0].trace_id)
                self.assertEqual(finishes[0].span_id, starts[0].span_id)
                self.assertEqual(
                    finishes[0].context_snapshot_id,
                    snapshots[0].snapshot_id,
                )
                self.assertEqual(
                    finishes[0].input_estimated_tokens,
                    starts[0].input_estimated_tokens,
                )

    def test_context_event_omits_private_reasoning_but_preserves_tool_arguments(self) -> None:
        ping = _tool("ping", category="agent")
        first = _call("ping", {"reasoning": "public tool argument"})
        first.reasoning_content = "provider private chain of thought"
        session = AgentSession(
            session_id="sid-private-reasoning",
            llm=_ScriptedLLM([first, ChatResult(content="完成")]),
            executor=ToolExecutor(tools=[ping]),
            tools_schema=[build_openai_schema(ping)],
            prompt_plan=prompt_plan("baseline"),
        )
        events: list[object] = []

        session.run_task(AgentTask(text="go"), on_event=events.append)

        contexts = [event for event in events if isinstance(event, ContextSnapshotPrepared)]
        self.assertEqual(len(contexts), 2)
        second = contexts[1]
        serialized = json.dumps(list(second.effective_messages), ensure_ascii=False, default=str)
        self.assertNotIn("provider private chain of thought", serialized)
        self.assertNotIn("reasoning_content", serialized)
        self.assertIn("public tool argument", serialized)
        self.assertEqual(second.coverage, "partial")
        self.assertIn("provider_private_reasoning", second.omitted)
        self.assertGreater(second.private_reasoning_omission_count, 0)

    def test_main_tool_spans_are_stamped(self) -> None:
        ping = _tool("ping", category="agent")
        llm = _ScriptedLLM([_call("ping", {}), ChatResult(content="完成")])
        session = AgentSession(
            session_id="sid",
            llm=llm,
            executor=ToolExecutor(tools=[ping]),
            tools_schema=[build_openai_schema(ping)],
            prompt_plan=prompt_plan("baseline"),
        )
        events = []
        session.run_task(
            AgentTask(text="go", metadata={"trace_id": "trace_fixed"}), on_event=events.append
        )

        started = [e for e in events if isinstance(e, ToolStarted)]
        finished = [e for e in events if isinstance(e, ToolFinished)]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].trace_id, "trace_fixed")
        self.assertEqual(started[0].depth, 0)
        self.assertIsNotNone(started[0].span_id)
        self.assertEqual(started[0].parent_span_id, finished[0].parent_span_id)
        llm_calls = [e for e in events if isinstance(e, LlmCallFinished)]
        llm_starts = [e for e in events if isinstance(e, LlmCallStarted)]
        contexts = [e for e in events if isinstance(e, ContextSnapshotPrepared)]
        self.assertEqual(len(llm_starts), len(llm_calls))
        self.assertEqual(len(contexts), len(llm_calls))
        self.assertEqual(contexts[0].coverage, "exact_model_input")
        self.assertEqual(contexts[0].backend, "native")
        self.assertEqual(contexts[0].span_id, llm_starts[0].span_id)
        self.assertEqual(contexts[0].snapshot_id, llm_starts[0].context_snapshot_id)
        self.assertEqual(contexts[0].effective_messages[0]["role"], "system")
        rendered = "\n".join(
            str(message.get("content") or "") for message in contexts[0].effective_messages
        )
        self.assertIn("baseline", rendered)
        self.assertEqual(len(contexts[0].tool_schemas), 1)
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
                    {"objective": "看趋势", "write_scope": "tests"},
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
            prompt_plan=prompt_plan("baseline"),
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
        subagent_contexts = [
            event
            for event in events
            if isinstance(event, ContextSnapshotPrepared) and event.depth > 0
        ]
        self.assertTrue(subagent_contexts)
        self.assertTrue(all(event.trace_id == "T" for event in subagent_contexts))
        self.assertTrue(all(event.parent_span_id for event in subagent_contexts))
        starts_by_snapshot = {
            event.context_snapshot_id: event
            for event in events
            if isinstance(event, LlmCallStarted)
        }
        self.assertTrue(
            all(
                starts_by_snapshot[event.snapshot_id].span_id == event.span_id
                for event in subagent_contexts
            )
        )


if __name__ == "__main__":
    unittest.main()
