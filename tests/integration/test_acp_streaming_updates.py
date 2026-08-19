"""Regression tests for ACP streaming updates in the Feishu bridge."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from chatcopilot.agent.protocol import (
    AgentResult,
    AgentTask,
    DeferredLifecycleIntent,
    FinalText,
    TextDelta,
    ToolFinished,
    ToolStarted,
)
from chatcopilot.middleware.access_control import AssistantMode
from chatcopilot.middleware.acp import server as acp_server
from chatcopilot.middleware.acp.lifecycle_barrier import LifecycleExecutionResult
from chatcopilot.middleware.acp.server import AcpChatAgent


class _FakeConn:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


class _FakeAgentSession:
    def __init__(
        self,
        *,
        emit_tool_progress: bool,
        emit_stream_text: bool,
        emit_final_text: bool,
        return_text: str,
        lifecycle_intents: tuple[DeferredLifecycleIntent, ...] = (),
    ) -> None:
        self.emit_tool_progress = emit_tool_progress
        self.emit_stream_text = emit_stream_text
        self.emit_final_text = emit_final_text
        self.return_text = return_text
        self.lifecycle_intents = lifecycle_intents

    def run_task(self, task: AgentTask, *, on_event) -> AgentResult:
        if self.emit_stream_text:
            on_event(TextDelta(text="你"))
            on_event(TextDelta(text="好"))
        if self.emit_tool_progress:
            on_event(ToolStarted(name="long_running_export", arguments={"items": ["resource-1"]}))
            on_event(ToolFinished(name="long_running_export", ok=True, summary="已加入后台队列"))
        if self.emit_final_text:
            on_event(FinalText(text=self.return_text))
        return AgentResult(
            final_text=self.return_text,
            stop_reason="end_turn",
            message_count=1,
            lifecycle_intents=self.lifecycle_intents,
        )


class _FakeSession:
    """模拟 SessionState 的接口：暴露 .session（AgentSession-like）+ debug_mode + workspace。"""

    def __init__(
        self,
        *,
        debug_mode: bool = False,
        emit_tool_progress: bool = False,
        emit_stream_text: bool = True,
        emit_final_text: bool = True,
        return_text: str = "你好",
        lifecycle_intents: tuple[DeferredLifecycleIntent, ...] = (),
        lifecycle_barrier: Any = None,
    ) -> None:
        self.is_materialized = True
        self.debug_mode = debug_mode
        self.assistant_mode = AssistantMode.PERFORMANCE
        self.lifecycle_barrier = lifecycle_barrier
        self.workspace = SimpleNamespace(
            root="",
            chat_kind="p2p",
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            attachments=SimpleNamespace(),
        )
        self.session = _FakeAgentSession(
            emit_tool_progress=emit_tool_progress,
            emit_stream_text=emit_stream_text,
            emit_final_text=emit_final_text,
            return_text=return_text,
            lifecycle_intents=lifecycle_intents,
        )

    def message_count(self) -> int:
        return 1

    def require_session(self) -> _FakeAgentSession:
        return self.session

    def persist_transcript(self) -> None:
        return None


class StreamingUpdateTests(unittest.TestCase):
    def _run_prompt(
        self,
        session: _FakeSession,
        *,
        tracking_available: bool = True,
    ) -> list[Any]:
        async def run_case() -> list[Any]:
            original_update = acp_server.update_agent_message_text
            original_refresh = acp_server._refresh_session_system_prompt
            original_latest_workspace = acp_server._latest_workspace_from_session_env
            acp_server.update_agent_message_text = lambda text: text
            acp_server._refresh_session_system_prompt = lambda _session: None
            acp_server._latest_workspace_from_session_env = lambda _workspace, *, platform_type: None
            try:
                agent = AcpChatAgent.__new__(AcpChatAgent)
                agent._sessions = {"sid": session}
                agent._conn = _FakeConn()
                agent._start_turn_task = (
                    (lambda **_kwargs: SimpleNamespace(task_id="task_test"))
                    if tracking_available
                    else (lambda **_kwargs: None)
                )
                agent._finish_turn_task = lambda *_args, **_kwargs: None
                agent._record_turn_event = lambda *_args, **_kwargs: None
                if session.lifecycle_barrier is not None:
                    agent._lifecycle_barrier = session.lifecycle_barrier

                async def _noop_replay(*_args, **_kwargs) -> None:
                    return None

                agent._send_unnotified_completed_jobs = _noop_replay

                await agent._prompt_locked(
                    [{"type": "text", "text": "hello"}],
                    "sid",
                    "mid",
                )
                return agent._conn.updates
            finally:
                acp_server.update_agent_message_text = original_update
                acp_server._refresh_session_system_prompt = original_refresh
                acp_server._latest_workspace_from_session_env = original_latest_workspace

        return asyncio.run(run_case())

    def test_non_debug_streaming_sends_only_final_update(self) -> None:
        updates = self._run_prompt(_FakeSession(debug_mode=False))

        self.assertEqual(updates, [("sid", "你好")])

    def test_debug_streaming_sends_only_final_text_update(self) -> None:
        updates = self._run_prompt(_FakeSession(debug_mode=True))

        self.assertEqual(updates, [("sid", "你好")])

    def test_task_tracking_failure_refuses_agent_execution(self) -> None:
        session = _FakeSession(debug_mode=False)

        updates = self._run_prompt(session, tracking_available=False)

        self.assertEqual(
            updates,
            [("sid", "任务跟踪不可用，消息未交给 Agent 处理；请让维护者检查任务存储。")],
        )

    def test_debug_tool_progress_keeps_final_text_update(self) -> None:
        updates = self._run_prompt(
            _FakeSession(debug_mode=True, emit_tool_progress=True)
        )

        self.assertEqual(len(updates), 3)
        self.assertIn("正在调用工具 `long_running_export`", updates[0][1])
        self.assertIn("`long_running_export` 完成", updates[1][1])
        self.assertEqual(updates[2], ("sid", "你好"))

    def test_non_debug_tool_progress_sends_visible_keepalive(self) -> None:
        updates = self._run_prompt(
            _FakeSession(
                debug_mode=False,
                emit_tool_progress=True,
                return_text="最终回复",
            )
        )

        self.assertEqual(updates, [("sid", "你好"), ("sid", "最终回复")])

    def test_final_return_text_is_used_when_emit_text_is_missing(self) -> None:
        updates = self._run_prompt(
            _FakeSession(emit_stream_text=False, emit_final_text=False, return_text="兜底回复")
        )

        self.assertEqual(updates, [("sid", "兜底回复")])

    def test_empty_turn_gets_user_visible_fallback(self) -> None:
        updates = self._run_prompt(
            _FakeSession(emit_stream_text=False, emit_final_text=False, return_text="")
        )

        self.assertEqual(len(updates), 1)
        self.assertIn("没有生成有效回复", updates[0][1])

    def test_lifecycle_intent_runs_after_final_update(self) -> None:
        calls: list[dict[str, Any]] = []

        class _Barrier:
            async def execute(self, intents, *, final_text_delivered: bool, workspace=None, session_id=None):
                calls.append(
                    {
                        "intents": intents,
                        "final_text_delivered": final_text_delivered,
                        "workspace": workspace,
                        "session_id": session_id,
                    }
                )
                return LifecycleExecutionResult(
                    status="started",
                    message="已开始自动更新重启，任务 ID: job_20260703_123456_abcdef12",
                    job_id="job_20260703_123456_abcdef12",
                )

        intent = DeferredLifecycleIntent(
            name="finalize_self_update",
            arguments={"reason": "修复测试"},
            source="main",
        )
        updates = self._run_prompt(
            _FakeSession(
                return_text="最终回复",
                lifecycle_intents=(intent,),
                lifecycle_barrier=_Barrier(),
            )
        )

        self.assertEqual(updates[0], ("sid", "最终回复"))
        self.assertIn("job_20260703_123456_abcdef12", updates[1][1])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["final_text_delivered"])
        self.assertEqual(calls[0]["intents"], (intent,))
        self.assertIsNotNone(calls[0]["workspace"])
        self.assertEqual(calls[0]["session_id"], "sid")

    def test_lifecycle_intent_skips_when_final_text_not_delivered(self) -> None:
        calls: list[dict[str, Any]] = []

        class _Barrier:
            async def execute(self, intents, *, final_text_delivered: bool, workspace=None, session_id=None):
                calls.append({"final_text_delivered": final_text_delivered, "workspace": workspace, "session_id": session_id})
                return LifecycleExecutionResult(
                    status="skipped",
                    message="final response was not delivered; lifecycle action was not started",
                )

        intent = DeferredLifecycleIntent(
            name="finalize_self_update",
            arguments={"reason": "修复测试"},
            source="main",
        )
        updates = self._run_prompt(
            _FakeSession(
                emit_stream_text=False,
                emit_final_text=False,
                return_text="",
                lifecycle_intents=(intent,),
                lifecycle_barrier=_Barrier(),
            )
        )

        self.assertIn("没有生成有效回复", updates[0][1])
        self.assertIn("自动更新重启已跳过", updates[1][1])
        self.assertEqual(calls[0]["final_text_delivered"], False)
        self.assertIsNotNone(calls[0]["workspace"])
        self.assertEqual(calls[0]["session_id"], "sid")


if __name__ == "__main__":
    unittest.main()
