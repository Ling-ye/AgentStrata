"""AgentSession 行为校验：工具回调、空回复 fallback、事件流。"""
from __future__ import annotations

from tests.prompt_plan_fixture import prompt_plan

import json
import unittest

from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.topic import TopicDecision
from chatcopilot.agent.protocol import (
    AgentTask,
    DeferredLifecycleIntent,
    FinalText,
    LlmCallFinished,
    TopicDecisionMade,
)
from chatcopilot.agent.lifecycle import defer_lifecycle_intent
from chatcopilot.agent.rag import RagHit
from chatcopilot.agent.session import AgentSession, _EMPTY_MODEL_REPLY_TEXT
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.external_tools.shared.tool_spec import ToolDef, build_openai_schema


class _FakeLLM:
    def __init__(self, results: list[ChatResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []
        self.model = "fake-model"

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            return ChatResult(content="")
        return self.results.pop(0)


class _FakeRetriever:
    def __init__(self, hits: list[RagHit]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    def search(self, query: str, *, top_k: int = 4) -> list[RagHit]:
        self.queries.append(query)
        return self.hits[:top_k]


class _FakeTopicClassifier:
    def __init__(self, decision: TopicDecision) -> None:
        self.decision = decision
        self.calls: list[dict] = []

    def classify(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": "call_tool",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _make_session(llm: _FakeLLM, tools: list[ToolDef]) -> AgentSession:
    return AgentSession(
        session_id="sid",
        llm=llm,
        executor=ToolExecutor(tools=list(tools)),
        tools_schema=[build_openai_schema(tool) for tool in tools],
        prompt_plan=prompt_plan("system baseline"),
    )


class AgentSessionTests(unittest.TestCase):
    def test_tool_summary_is_returned_when_followup_model_reply_is_empty(self) -> None:
        def handler(args: dict):
            return ("已加入全局迁移队列，任务 ID: job-1。", [], None)

        tool = ToolDef(
            name="submit_job",
            summary="提交后台任务",
            properties={},
            required=[],
            handler=handler,
        )
        session = _make_session(
            _FakeLLM([
                ChatResult(tool_calls=[_tool_call("submit_job", {})]),
                ChatResult(content=""),
            ]),
            [tool],
        )
        final_texts: list[str] = []

        def on_event(event):
            if isinstance(event, FinalText):
                final_texts.append(event.text)

        result = session.run_task(AgentTask(text="执行迁移"), on_event=on_event)

        self.assertEqual(result.final_text, "已加入全局迁移队列，任务 ID: job-1。")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertIn("已加入全局迁移队列，任务 ID: job-1。", final_texts)

    def test_tool_call_cap_summarizes_collected_tool_evidence(self) -> None:
        def handler(args: dict):
            return ("已定位到 stop_reason=tool_call_cap。", [], None)

        tool = ToolDef(
            name="get_task_status",
            summary="查询 task 状态",
            properties={},
            required=[],
            handler=handler,
        )
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_task_status", "arguments": "{}"},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "get_task_status", "arguments": "{}"},
            },
        ]
        session = _make_session(_FakeLLM([ChatResult(tool_calls=calls)]), [tool])
        session.max_tool_calls = 1

        result = session.run_task(AgentTask(text="查任务失败原因"), on_event=lambda _event: None)

        self.assertEqual(result.stop_reason, "tool_call_cap")
        self.assertIn("已收集到的证据", result.final_text)
        self.assertIn("stop_reason=tool_call_cap", result.final_text)

    def test_empty_model_reply_gets_user_visible_fallback(self) -> None:
        session = _make_session(
            _FakeLLM([ChatResult(content="", finish_reason="stop")]),
            [],
        )
        final_texts: list[str] = []

        def on_event(event):
            if isinstance(event, FinalText):
                final_texts.append(event.text)

        result = session.run_task(AgentTask(text="读取文档"), on_event=on_event)

        self.assertEqual(result.final_text, _EMPTY_MODEL_REPLY_TEXT)
        self.assertEqual(final_texts, [_EMPTY_MODEL_REPLY_TEXT])
        self.assertEqual(session.snapshot_messages()[-1]["content"], _EMPTY_MODEL_REPLY_TEXT)

    def test_tool_lifecycle_events_dispatched(self) -> None:
        def handler(args: dict):
            return ("done", ["/tmp/out.txt"], None)

        tool = ToolDef(
            name="produce",
            summary="produce a file",
            properties={},
            required=[],
            handler=handler,
        )
        session = _make_session(
            _FakeLLM([
                ChatResult(tool_calls=[_tool_call("produce", {"x": 1})]),
                ChatResult(content="完成"),
            ]),
            [tool],
        )
        events: list = []

        result = session.run_task(AgentTask(text="跑一下"), on_event=events.append)

        kinds = [type(event).__name__ for event in events]
        self.assertIn("ToolStarted", kinds)
        self.assertIn("ToolFinished", kinds)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.final_text, "完成")
        self.assertTrue(any(p.path == "/tmp/out.txt" for p in result.produced_resources))

    def test_non_artifact_tool_outputs_are_not_recorded_as_resources(self) -> None:
        def handler(args: dict):
            return ("found", ["src/app.py:1:old text"], None)

        tool = ToolDef(
            name="search",
            summary="search text",
            properties={},
            required=[],
            handler=handler,
            artifact_kinds=(),
        )
        session = _make_session(
            _FakeLLM([
                ChatResult(tool_calls=[_tool_call("search", {})]),
                ChatResult(content="完成"),
            ]),
            [tool],
        )

        result = session.run_task(AgentTask(text="搜索"), on_event=lambda _: None)

        self.assertEqual(result.produced_resources, ())

    def test_dev_write_requires_finalize_before_final_answer(self) -> None:
        def edit_handler(args: dict):
            return ("已修改文件", [], None)

        def finalize_handler(args: dict):
            return ("已提交自更新任务。job_id: job-1", ["/tmp/jobs/job-1"], None)

        edit_tool = ToolDef(
            name="edit_file",
            summary="edit file",
            properties={},
            required=[],
            handler=edit_handler,
        )
        finalize_tool = ToolDef(
            name="finalize_self_update",
            summary="finalize update",
            properties={"reason": {"type": "string", "description": "reason"}},
            required=["reason"],
            handler=finalize_handler,
            artifact_kinds=("directory",),
        )
        llm = _FakeLLM(
            [
                ChatResult(tool_calls=[_tool_call("edit_file", {})]),
                ChatResult(content="错误地提前完成"),
                ChatResult(content="已完成", tool_calls=[_tool_call("finalize_self_update", {"reason": "修复测试"})]),
                ChatResult(content=""),
            ]
        )
        session = _make_session(llm, [edit_tool, finalize_tool])
        final_texts: list[str] = []

        result = session.run_task(
            AgentTask(text="改一个文件"),
            on_event=lambda event: final_texts.append(event.text) if isinstance(event, FinalText) else None,
        )

        self.assertEqual(result.final_text, "已完成")
        self.assertEqual(final_texts, ["已完成"])
        self.assertEqual(len(llm.calls), 4)
        self.assertEqual(len(result.lifecycle_intents), 1)
        self.assertEqual(result.lifecycle_intents[0].name, "finalize_self_update")
        self.assertTrue(
            any(
                "[SELF-UPDATE REQUIRED]" in (message.get("content") or "")
                for message in llm.calls[2]["messages"]
            )
        )

    def test_dev_write_blocks_submit_result_until_finalize(self) -> None:
        submitted: list[dict] = []

        def edit_handler(args: dict):
            return ("已修改文件", [], None)

        def finalize_handler(args: dict):
            return ("已提交自更新任务。job_id: job-1", ["/tmp/jobs/job-1"], None)

        def submit_handler(args: dict):
            submitted.append(args)
            return ("structured result submitted", [], None)

        edit_tool = ToolDef(
            name="edit_file",
            summary="edit file",
            properties={},
            required=[],
            handler=edit_handler,
        )
        finalize_tool = ToolDef(
            name="finalize_self_update",
            summary="finalize update",
            properties={"reason": {"type": "string", "description": "reason"}},
            required=["reason"],
            handler=finalize_handler,
        )
        submit_tool = ToolDef(
            name="submit_result",
            summary="submit result",
            properties={"summary": {"type": "string", "description": "summary"}},
            required=["summary"],
            handler=submit_handler,
        )
        llm = _FakeLLM(
            [
                ChatResult(tool_calls=[_tool_call("edit_file", {})]),
                ChatResult(tool_calls=[_tool_call("submit_result", {"summary": "提前提交"})]),
                ChatResult(content="最终总结", tool_calls=[_tool_call("finalize_self_update", {"reason": "修复测试"})]),
                ChatResult(tool_calls=[_tool_call("submit_result", {"summary": "最终提交"})]),
                ChatResult(content="已完成"),
            ]
        )
        session = _make_session(llm, [edit_tool, finalize_tool, submit_tool])

        result = session.run_task(AgentTask(text="改一个文件"), on_event=lambda _: None)

        self.assertEqual(result.final_text, "已完成")
        self.assertEqual(submitted, [{"summary": "最终提交"}])
        self.assertEqual(len(result.lifecycle_intents), 1)

    def test_finalize_self_update_requires_user_visible_summary(self) -> None:
        called: list[dict] = []

        def finalize_handler(args: dict):
            called.append(args)
            return ("should not run", [], None)

        finalize_tool = ToolDef(
            name="finalize_self_update",
            summary="finalize update",
            properties={"reason": {"type": "string", "description": "reason"}},
            required=["reason"],
            handler=finalize_handler,
        )
        llm = _FakeLLM(
            [
                ChatResult(tool_calls=[_tool_call("finalize_self_update", {"reason": "修复测试"})]),
                ChatResult(tool_calls=[_tool_call("finalize_self_update", {"reason": "修复测试"})]),
                ChatResult(tool_calls=[_tool_call("finalize_self_update", {"reason": "修复测试"})]),
            ]
        )
        session = _make_session(llm, [finalize_tool])

        result = session.run_task(AgentTask(text="发布更新"), on_event=lambda _: None)

        self.assertEqual(result.stop_reason, "tool_failure_cap")
        self.assertEqual(called, [])
        self.assertEqual(result.lifecycle_intents, ())

    def test_finalize_self_update_rejects_duplicate_lifecycle_intent(self) -> None:
        finalize_tool = ToolDef(
            name="finalize_self_update",
            summary="finalize update",
            properties={"reason": {"type": "string", "description": "reason"}},
            required=["reason"],
            handler=lambda _args: ("should not run", [], None),
        )
        llm = _FakeLLM(
            [
                ChatResult(
                    content="已完成修改",
                    tool_calls=[_tool_call("finalize_self_update", {"reason": "第一次"})],
                ),
                ChatResult(
                    content="重复发布",
                    tool_calls=[_tool_call("finalize_self_update", {"reason": "第二次"})],
                ),
                ChatResult(content="最终总结"),
            ]
        )
        session = _make_session(llm, [finalize_tool])

        result = session.run_task(AgentTask(text="发布更新"), on_event=lambda _: None)

        self.assertEqual(result.final_text, "最终总结")
        self.assertEqual(len(result.lifecycle_intents), 1)
        self.assertEqual(result.lifecycle_intents[0].arguments["reason"], "第一次")

    def test_delegate_tool_can_bubble_lifecycle_intent_structurally(self) -> None:
        def delegate_handler(args: dict):
            defer_lifecycle_intent(
                DeferredLifecycleIntent(
                    name="finalize_self_update",
                    arguments={"reason": "subagent 完成自更新"},
                    source="subagent",
                )
            )
            return ("developer subagent completed", [], None)

        delegate_tool = ToolDef(
            name="delegate_developer",
            summary="delegate developer",
            properties={},
            required=[],
            handler=delegate_handler,
        )
        llm = _FakeLLM(
            [
                ChatResult(content="已完成修改", tool_calls=[_tool_call("delegate_developer", {})]),
                ChatResult(content="最终总结"),
            ]
        )
        session = _make_session(llm, [delegate_tool])

        result = session.run_task(AgentTask(text="委托开发"), on_event=lambda _: None)

        self.assertEqual(result.final_text, "最终总结")
        self.assertEqual(len(result.lifecycle_intents), 1)
        self.assertEqual(result.lifecycle_intents[0].source, "subagent")

    def test_failed_delegate_tool_does_not_commit_bubbled_lifecycle_intent(self) -> None:
        def delegate_handler(args: dict):
            defer_lifecycle_intent(
                DeferredLifecycleIntent(
                    name="finalize_self_update",
                    arguments={"reason": "subagent 失败前登记"},
                    source="subagent",
                )
            )
            raise RuntimeError("delegate failed")

        delegate_tool = ToolDef(
            name="delegate_developer",
            summary="delegate developer",
            properties={},
            required=[],
            handler=delegate_handler,
        )
        llm = _FakeLLM(
            [
                ChatResult(content="尝试委托", tool_calls=[_tool_call("delegate_developer", {})]),
                ChatResult(content="委托失败，未发布"),
            ]
        )
        session = _make_session(llm, [delegate_tool])

        result = session.run_task(AgentTask(text="委托开发"), on_event=lambda _: None)

        self.assertEqual(result.final_text, "委托失败，未发布")
        self.assertEqual(result.lifecycle_intents, ())

    def test_llm_usage_event_dispatched(self) -> None:
        session = _make_session(
            _FakeLLM(
                [
                    ChatResult(
                        content="完成",
                        finish_reason="stop",
                        usage={
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                            "cached_tokens": 40,
                            "cache_read_tokens": 40,
                            "cache_write_tokens": 0,
                        },
                    )
                ]
            ),
            [],
        )
        events: list = []

        session.run_task(AgentTask(text="统计 usage"), on_event=events.append)

        usage_events = [event for event in events if isinstance(event, LlmCallFinished)]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].model, "fake-model")
        self.assertEqual(usage_events[0].iteration, 0)
        self.assertEqual(usage_events[0].usage["cached_tokens"], 40)

    def test_unrelated_topic_view_does_not_clear_transcript(self) -> None:
        llm = _FakeLLM([ChatResult(content="天气晴")])
        classifier = _FakeTopicClassifier(
            TopicDecision.unrelated(source="llm", reason="standalone weather question")
        )
        session = AgentSession(
            session_id="sid",
            llm=llm,
            executor=ToolExecutor(tools=[]),
            tools_schema=[],
            prompt_plan=prompt_plan("system baseline"),
            context_manager=ContextManager(max_context_tokens=50000, sliding_window_turns=10),
            topic_classifier=classifier,
        )
        session.record_exchange("分析 csv", "分析完成")
        events: list = []

        result = session.run_task(AgentTask(text="北京明天天气怎么样？"), on_event=events.append)

        self.assertEqual(result.final_text, "天气晴")
        sent_messages = llm.calls[0]["messages"]
        sent_user_messages = [m["content"] for m in sent_messages if m.get("role") == "user"]
        self.assertEqual(sent_user_messages, ["北京明天天气怎么样？"])
        full_user_messages = [
            m["content"] for m in session.snapshot_messages() if m.get("role") == "user"
        ]
        self.assertEqual(full_user_messages, ["分析 csv", "北京明天天气怎么样？"])
        self.assertTrue(any(isinstance(event, TopicDecisionMade) for event in events))

    def test_rag_hits_are_appended_to_current_user_message(self) -> None:
        llm = _FakeLLM([ChatResult(content="收到")])
        session = _make_session(llm, [])
        session.retriever = _FakeRetriever(
            [RagHit(source="docs/phones.md", chunk_id=1, text="2026 手机发售以厂商公告为准。", score=3.0)]
        )

        result = session.run_task(AgentTask(text="查 2026 手机 发售"), on_event=lambda _event: None)

        self.assertEqual(result.final_text, "收到")
        self.assertEqual(session.retriever.queries, ["查 2026 手机 发售"])
        user_message = llm.calls[0]["messages"][1]["content"]
        self.assertIn("相关知识库片段", user_message)
        self.assertIn("docs/phones.md#chunk-1", user_message)
        self.assertIn("不是联网搜索结果", user_message)

    def test_empty_rag_hits_do_not_pollute_user_message(self) -> None:
        llm = _FakeLLM([ChatResult(content="收到")])
        session = _make_session(llm, [])
        session.retriever = _FakeRetriever([])

        session.run_task(AgentTask(text="普通问题"), on_event=lambda _event: None)

        user_message = llm.calls[0]["messages"][1]["content"]
        self.assertNotIn("相关知识库片段", user_message)


class RepairOrphanToolCallsTests(unittest.TestCase):
    """_repair_orphan_tool_calls 应为缺失 tool result 的 tool_calls 补全合成消息。"""

    def test_no_orphans_leaves_messages_unchanged(self) -> None:
        messages = [
            {"role": "system", "content": "hi"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "t1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        count = AgentSession._repair_orphan_tool_calls(messages)
        self.assertEqual(count, 0)
        self.assertEqual(len(messages), 5)

    def test_single_orphan_gets_synthetic_result(self) -> None:
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "t1", "content": "ok"},
            {"role": "assistant", "content": "timeout"},
        ]
        count = AgentSession._repair_orphan_tool_calls(messages)
        self.assertEqual(count, 1)
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "c2")
        self.assertIn("aborted", messages[2]["content"])
        self.assertEqual(messages[3]["role"], "assistant")
        self.assertEqual(messages[3]["content"], "timeout")

    def test_all_orphans_in_batch(self) -> None:
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
            ]},
            {"role": "assistant", "content": "timeout"},
        ]
        count = AgentSession._repair_orphan_tool_calls(messages)
        self.assertEqual(count, 2)
        self.assertEqual(messages[1]["role"], "tool")
        self.assertEqual(messages[2]["role"], "tool")
        ids = {messages[1]["tool_call_id"], messages[2]["tool_call_id"]}
        self.assertEqual(ids, {"c1", "c2"})

    def test_timeout_path_repairs_history(self) -> None:
        """Simulates the timeout scenario from task_20260628_204607."""
        def handler(args: dict):
            return ("ok", [], None)

        tool = ToolDef(
            name="search",
            summary="search",
            properties={"q": {"type": "string", "description": "query"}},
            required=["q"],
            handler=handler,
        )
        session = _make_session(
            _FakeLLM([
                ChatResult(tool_calls=[_tool_call("search", {"q": "test"})]),
                ChatResult(content="结果"),
            ]),
            [tool],
        )
        session.timeout_seconds = 0

        result = session.run_task(AgentTask(text="搜索"), on_event=lambda _: None)

        self.assertEqual(result.stop_reason, "timeout_cap")
        msgs = session.snapshot_messages()
        for msg in msgs:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                required_ids = {tc["id"] for tc in msg["tool_calls"]}
                j = msgs.index(msg) + 1
                found_ids = set()
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    found_ids.add(msgs[j]["tool_call_id"])
                    j += 1
                self.assertEqual(required_ids, found_ids,
                                 "every tool_call must have a matching tool result")


if __name__ == "__main__":
    unittest.main()
