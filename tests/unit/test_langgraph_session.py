from __future__ import annotations

from chatcopilot.core.config import LLMConfig

from tests.prompt_plan_fixture import prompt_input, prompt_plan, runtime_route

import importlib.util
import json
import unittest

from chatcopilot.core.config import ChatConfig
from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.contracts.agent import (
    AgentTask,
    FinalText,
    LlmCallFinished,
    ToolFinished,
    ToolStarted,
)
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.tools import (
    ToolContext,
    ToolDef,
    ToolResult,
    build_openai_schema,
    object_schema,
)


def _has_langgraph() -> bool:
    return importlib.util.find_spec("langgraph") is not None


class _FakeLLM:
    model = "fake-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            return ChatResult(content="")
        return self.results.pop(0)


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": "call_tool",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _make_tool() -> ToolDef:
    def handler(args: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary=f"tool ok: {args.get('value')}")

    return ToolDef(
        name="sample_tool",
        summary="sample tool",
        input_schema=object_schema(
            {"value": {"type": "string"}},
            required=("value",),
        ),
        output_schema=object_schema(),
        handler=handler,
    )


class LangGraphRuntimeTests(unittest.TestCase):
    def test_runtime_selects_langgraph_session_class(self) -> None:
        tool = _make_tool()
        runtime = AgentRuntime(
            main_model_client=_FakeLLM([]),  # type: ignore[arg-type]
            tools=(tool,),
            tools_schema=(build_openai_schema(tool),),
            runtime_config=ChatConfig(llm=LLMConfig(model="gpt-4o-mini"), ),
            route=runtime_route("langgraph"),
        )

        session = runtime.open_session(session_id="sid", prompt_input=prompt_input("system"))

        self.assertEqual(session.runtime_id, "langgraph")


@unittest.skipUnless(_has_langgraph(), "langgraph is not installed")
class LangGraphSessionTests(unittest.TestCase):
    def test_soft_budget_wraps_up_repeated_tools_but_allows_progress(self) -> None:
        for repeated, expected_calls, expected_stop in ((True, 5, "iteration_cap"), (False, 7, "end_turn")):
            with self.subTest(repeated=repeated):
                tool = _make_tool()
                llm = _FakeLLM([
                    *[ChatResult(tool_calls=[_tool_call("sample_tool", {"value": "same" if repeated else str(i)})])
                      for i in range(6)],
                    ChatResult(content="完成"),
                ])
                session = LangGraphAgentSession(
                    session_id="budget", llm=llm, executor=ToolExecutor(tools=[tool], caller_role_hint="owner"),
                    tools_schema=[build_openai_schema(tool)], prompt_plan=prompt_plan(),
                    resolved_runtime_id="langgraph", max_tool_iterations=1, hard_iteration_cap=10,
                )
                result = session.run_task(AgentTask("执行任务"), on_event=lambda _: None)
                self.assertEqual(result.stop_reason, expected_stop)
                self.assertEqual(len(llm.calls), expected_calls)

    def test_deadline_between_tools_prevents_later_side_effect_and_repairs_history(self) -> None:
        executed = []
        tool = ToolDef(
            name="write_marker", summary="write marker", input_schema=object_schema(), output_schema=object_schema(),
            handler=lambda _args, _context: (executed.append(True) or ToolResult(ok=True, summary="written")),
        )
        calls = [{**_tool_call("write_marker", {}), "id": f"call-{i}"} for i in range(2)]
        session = LangGraphAgentSession(
            session_id="deadline", llm=_FakeLLM([ChatResult(tool_calls=calls)]),
            executor=ToolExecutor(tools=[tool], caller_role_hint="owner"), tools_schema=[build_openai_schema(tool)],
            prompt_plan=prompt_plan(), resolved_runtime_id="langgraph", hard_timeout_seconds=10,
        )
        session._hard_timed_out = lambda _started: bool(executed)
        result = session.run_task(AgentTask("write twice"), on_event=lambda _: None)
        self.assertEqual(result.stop_reason, "timeout_cap")
        self.assertEqual(executed, [True])
        pending = [m for m in session.snapshot_messages() if m.get("tool_call_id") == "call-1"]
        self.assertEqual(len(pending), 1)
        self.assertFalse(json.loads(pending[0]["content"])["ok"])

    def test_tool_loop_runs_through_state_graph(self) -> None:
        tool = _make_tool()
        llm = _FakeLLM(
            [
                ChatResult(tool_calls=[_tool_call("sample_tool", {"value": "x"})]),
                ChatResult(content="完成"),
            ]
        )
        session = LangGraphAgentSession(
            session_id="sid",
            llm=llm,  # type: ignore[arg-type]
            executor=ToolExecutor(caller_role_hint="owner", tools=[tool]),
            tools_schema=[build_openai_schema(tool)],
            prompt_plan=prompt_plan("system", runtime_id="langgraph"),
            resolved_runtime_id="langgraph",
        )
        events: list[object] = []

        result = session.run_task(AgentTask(text="run tool"), on_event=events.append)

        self.assertEqual(result.final_text, "完成")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(any(isinstance(event, ToolStarted) for event in events))
        self.assertTrue(any(isinstance(event, ToolFinished) for event in events))
        self.assertTrue(any(isinstance(event, FinalText) and event.text == "完成" for event in events))
        llm_events = [event for event in events if isinstance(event, LlmCallFinished)]
        self.assertEqual(len(llm_events), 2)
        self.assertEqual(
            {event.runtime_id for event in events if hasattr(event, "runtime_id")},
            {"langgraph"},
        )
        self.assertEqual(llm_events[-1].context_kind, "sliding_window")
        self.assertEqual(session.snapshot_messages()[-1]["content"], "完成")
