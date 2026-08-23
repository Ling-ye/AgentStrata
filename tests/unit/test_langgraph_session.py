from __future__ import annotations

from tests.prompt_plan_fixture import prompt_input, prompt_plan

import importlib.util
import json
import unittest

from chatcopilot.core.config import ChatConfig
from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.backends import BackendAgentSession
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
            llm=_FakeLLM([]),  # type: ignore[arg-type]
            tools=(tool,),
            tools_schema=(build_openai_schema(tool),),
            runtime_config=ChatConfig(),
            agent_backend="langgraph",
        )

        session = runtime.new_session(session_id="sid", prompt_input=prompt_input("system"))

        self.assertIsInstance(session, BackendAgentSession)
        self.assertEqual(session.backend_id, "langgraph")
        self.assertIsInstance(session.backend.native_session(session.backend_session_ref), LangGraphAgentSession)


@unittest.skipUnless(_has_langgraph(), "langgraph is not installed")
class LangGraphSessionTests(unittest.TestCase):
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
            executor=ToolExecutor(tools=[tool]),
            tools_schema=[build_openai_schema(tool)],
            prompt_plan=prompt_plan("system"),
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
        self.assertEqual(llm_events[-1].context_kind, "sliding_window")
        self.assertEqual(session.snapshot_messages()[-1]["content"], "完成")
