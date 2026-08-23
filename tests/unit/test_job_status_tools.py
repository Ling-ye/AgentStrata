"""Regression tests for the four bug fixes around job status lookup:

1. ``_JOB_ID_RE`` must match when a Chinese character is adjacent to ``job_...``
   (CJK characters are word characters under Python's unicode ``\\b``, so the
   old pattern silently failed for "告诉我job_xxx" and the entire ACP short-
   circuit fell through to the LLM tool loop).
2. ``list_workspace`` must accept ``subdir="jobs"`` so the LLM can browse
   pending / running / finished background tasks via the workspace tool.
3. ``read_text_head`` must reject a directory path with ``IsADirectoryError``
   and tell the LLM to use ``get_job_status`` / ``list_workspace`` instead of
   silently failing with ``FileNotFoundError`` (which the LLM previously
   misread as "the job hasn't started yet").
4. The new ``get_job_status`` tool must read ``jobs/<id>/status.json`` plus
   the tail of ``stdout.log`` so users actually see progress, not just
   ``state=running`` with no detail.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from chatcopilot.agent.tools.builtin.workspace_tools import (
    _handler_get_job_status,
    _handler_get_task_status,
    _handler_list_workspace,
    _handler_read_text_head,
)
from chatcopilot.contracts.tools import ToolContext
from chatcopilot.core.workspace_context import bind_workspace_service
from chatcopilot.middleware.acp import server as acp_server
from chatcopilot.middleware.acp.job_dispatch import extract_job_status_query
from chatcopilot.middleware.acp.task_dispatch import extract_task_status_query
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.core.workspace_runtime.model import describe_workspace as _model_describe


class _StubWorkspaceService:
    """Minimal workspace service for test isolation."""
    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    def resolve_workspace(self, *, create: bool = True) -> Workspace:
        return self._ws

    def resolve_workspace_root(self, workspace=None):
        return self._ws.root

    def cleanup_workspace(self, workspace) -> None:
        pass

    def describe_workspace(self, workspace) -> str:
        return _model_describe(workspace)

    def list_workspace_inventories(self, root):
        return []


VALID_JOB_ID = "job_20260528_120125_10a50d0c"
VALID_TASK_ID = "task_20260703_165921_f667f168"


def _make_workspace(tmp: Path) -> Workspace:
    return Workspace(
        root=tmp,
        chat_kind="p2p",
        chat_id=None,
        user_id="ou_test",
    ).ensure()


def _seed_job_dir(ws: Workspace, *, status: str, message: str, stdout_lines: list[str]) -> Path:
    job_dir = ws.root / "jobs" / VALID_JOB_ID
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "request.json").write_text(
        json.dumps(
            {
                "job_id": VALID_JOB_ID,
                "tool_name": "long_running_export",
                "args": {},
                "execution_policy": "global_serial_background",
                "queue_name": "datasource_global",
                "submitted_at": time.time(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_dir / "status.json").write_text(
        json.dumps({"status": status, "message": message, "updated_at": time.time()}),
        encoding="utf-8",
    )
    (job_dir / "stdout.log").write_text("\n".join(stdout_lines), encoding="utf-8")
    return job_dir


def _seed_task_dir(
    ws: Workspace,
    *,
    status: str = "succeeded",
    stop_reason: str = "tool_call_cap",
    final_text: str = "（已达单轮工具调用上限 16 次，停止本轮以避免失控。）",
) -> Path:
    task_dir = ws.root / "tasks" / VALID_TASK_ID
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": VALID_TASK_ID,
                "description": "检查任务失败原因",
                "progress": "已完成回答。",
                "status": status,
                "tools": [
                    {"name": "get_job_status", "status": "succeeded"},
                    {"name": "list_workspace", "status": "succeeded"},
                ],
                "llm_calls": [{"model": "test", "iteration": 0}],
                "usage_totals": {"total_tokens": 1234},
                "job_ids": [VALID_JOB_ID],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (task_dir / "turn.json").write_text(
        json.dumps(
            {
                "task_id": VALID_TASK_ID,
                "user_text": "检查任务失败原因",
                "final_text": final_text,
                "stop_reason": stop_reason,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (task_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"event": "llm_call_finished", "data": {}}, ensure_ascii=False),
                json.dumps({"event": "tool_finished", "data": {}}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return task_dir


class JobIdRegexUnicodeBoundaryTests(unittest.TestCase):
    """Bug 0: ``_JOB_ID_RE`` must match across CJK / ASCII boundaries.

    Previously the regex used ``\\b`` which treats CJK characters as word
    characters under Python's default ``re`` engine, so the boundary between
    ``我`` and ``j`` was not a word boundary and the whole match failed.
    """

    def test_chinese_immediately_before_job_id_still_matches(self) -> None:
        text = f"告诉我{VALID_JOB_ID}处理完了吗？"
        self.assertEqual(extract_job_status_query(text), VALID_JOB_ID)

    def test_chinese_intent_only_without_id_returns_none(self) -> None:
        self.assertIsNone(extract_job_status_query("任务完了吗？"))

    def test_ascii_boundary_still_works(self) -> None:
        text = f"{VALID_JOB_ID} 这个完了吗"
        self.assertEqual(extract_job_status_query(text), VALID_JOB_ID)

    def test_id_embedded_in_longer_token_is_rejected(self) -> None:
        # Lookbehind / lookahead must still refuse matches glued to ASCII
        # word characters (otherwise random hex blobs could collide).
        glued = f"prefix{VALID_JOB_ID}suffix"
        self.assertIsNone(extract_job_status_query(glued))


class TaskIdRegexUnicodeBoundaryTests(unittest.TestCase):
    def test_chinese_immediately_before_task_id_still_matches(self) -> None:
        text = f"帮我查{VALID_TASK_ID}为什么失败"
        self.assertEqual(extract_task_status_query(text), VALID_TASK_ID)

    def test_id_embedded_in_longer_token_is_rejected(self) -> None:
        glued = f"prefix{VALID_TASK_ID}suffix"
        self.assertIsNone(extract_task_status_query(glued))


class ListWorkspaceAcceptsJobsSubdirTests(unittest.TestCase):
    """Bug 1: ``list_workspace`` must accept ``subdir="jobs"``."""

    def test_jobs_subdir_lists_job_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            _seed_job_dir(
                ws,
                status="running",
                message="任务正在执行",
                stdout_lines=["[1/100] cell A1: OK"],
            )

            with bind_workspace_service(_StubWorkspaceService(ws)):
                result = _handler_list_workspace(
                    {"subdir": "jobs", "recursive": True}, ToolContext()
                )

        self.assertTrue(result.ok)
        self.assertIn(VALID_JOB_ID, result.summary)
        self.assertTrue(result.outputs)
        self.assertEqual(result.data["subdir"], "jobs")

    def test_tasks_subdir_lists_task_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            _seed_task_dir(ws)

            with bind_workspace_service(_StubWorkspaceService(ws)):
                result = _handler_list_workspace(
                    {"subdir": "tasks", "recursive": True}, ToolContext()
                )

        self.assertTrue(result.ok)
        self.assertIn(VALID_TASK_ID, result.summary)
        self.assertTrue(result.outputs)
        self.assertEqual(result.data["subdir"], "tasks")


class ReadTextHeadRejectsDirectoryTests(unittest.TestCase):
    """Bug 2: directory path → ``IsADirectoryError`` with actionable hint."""

    def test_directory_path_raises_with_get_job_status_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            job_dir = _seed_job_dir(
                ws,
                status="running",
                message="任务正在执行",
                stdout_lines=["[1/3] OK"],
            )

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                with self.assertRaises(IsADirectoryError) as ctx:
                    _handler_read_text_head({"path": str(job_dir)}, ToolContext())
            finally:
                _wt.resolve_workspace = saved_resolver

        message = str(ctx.exception)
        self.assertIn("目录而非文件", message)
        # 错误信息必须能引导 LLM 切到正道工具，否则它会原样误读"任务不存在"。
        self.assertIn("get_job_status", message)


class GetJobStatusToolTests(unittest.TestCase):
    """New ``get_job_status`` tool: reads status.json + tail of stdout.log."""

    def test_running_job_returns_status_and_progress_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            _seed_job_dir(
                ws,
                status="running",
                message="任务正在执行。",
                stdout_lines=[
                    "[1/3288] cell A1: OK",
                    "[3134/3288] cell U793: 1 个文件",
                    "  [附件] 上传云盘中: 11pm-据点塔防.mov",
                    "  [分片上传] part 37/59 OK (4096 KB)",
                ],
            )

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_job_status(
                    {"job_id": VALID_JOB_ID, "tail_lines": 20}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertTrue(result.ok)
        self.assertIn(VALID_JOB_ID, result.summary)
        self.assertIn("状态: running", result.summary)
        self.assertIn("任务正在执行", result.summary)
        # 进度尾部必须出现，否则用户看不到"分片 37/59"这种关键信号。
        self.assertIn("part 37/59", result.summary)
        self.assertTrue(result.outputs)
        self.assertEqual(result.data["job_id"], VALID_JOB_ID)
        self.assertEqual(result.data["status"], "running")

    def test_missing_job_returns_friendly_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_job_status(
                    {"job_id": VALID_JOB_ID}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertFalse(result.ok)
        self.assertIn("找不到", result.error or "")
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.data["job_id"], VALID_JOB_ID)

    def test_invalid_job_id_format_returns_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_job_status(
                    {"job_id": "not_a_valid_id"}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertFalse(result.ok)
        self.assertIn("找不到", result.error or "")
        self.assertEqual(result.data["job_id"], "not_a_valid_id")

    def test_task_id_returns_get_task_status_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_job_status(
                    {"job_id": VALID_TASK_ID}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertFalse(result.ok)
        self.assertIn("单轮对话任务 ID", result.error or "")
        self.assertIn("get_task_status", result.error or "")
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.data["job_id"], VALID_TASK_ID)


class GetTaskStatusToolTests(unittest.TestCase):
    def test_tool_call_cap_task_returns_stop_reason_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            _seed_task_dir(ws)

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_task_status(
                    {"task_id": VALID_TASK_ID}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertTrue(result.ok)
        self.assertIn(VALID_TASK_ID, result.summary)
        self.assertIn("Stop reason: tool_call_cap", result.summary)
        self.assertIn("预算/上限停止", result.summary)
        self.assertIn("工具调用: 2 次", result.summary)
        self.assertIn(VALID_JOB_ID, result.summary)
        self.assertTrue(result.outputs)
        self.assertEqual(result.data["task_id"], VALID_TASK_ID)

    def test_failed_task_returns_failed_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            _seed_task_dir(ws, status="failed", stop_reason="error", final_text="执行失败")

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_task_status(
                    {"task_id": VALID_TASK_ID}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertTrue(result.ok)
        self.assertIn("状态: failed", result.summary)
        self.assertIn("task status=failed", result.summary)
        self.assertEqual(result.data["task_id"], VALID_TASK_ID)

    def test_missing_task_returns_friendly_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_task_status(
                    {"task_id": VALID_TASK_ID}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertFalse(result.ok)
        self.assertIn("找不到单轮任务", result.error or "")
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.data["task_id"], VALID_TASK_ID)

    def test_invalid_task_id_returns_format_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))

            from chatcopilot.agent.tools.builtin import workspace_tools as _wt
            saved_resolver = _wt.resolve_workspace
            _wt.resolve_workspace = lambda create=False: ws
            try:
                result = _handler_get_task_status(
                    {"task_id": "job_20260528_120125_10a50d0c"}, ToolContext()
                )
            finally:
                _wt.resolve_workspace = saved_resolver

        self.assertFalse(result.ok)
        self.assertIn("不是有效的单轮任务 ID", result.error or "")
        self.assertIn("get_job_status", result.error or "")
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.data["task_id"], "job_20260528_120125_10a50d0c")


class AcpTaskStatusShortcutTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_task_status_uses_deterministic_formatter(self) -> None:
        class _Conn:
            def __init__(self) -> None:
                self.updates = []

            async def session_update(self, *, session_id, update):
                self.updates.append((session_id, update))

        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_workspace(Path(tmp))
            _seed_task_dir(ws)
            agent = acp_server.AcpChatAgent.__new__(acp_server.AcpChatAgent)
            agent._conn = _Conn()
            session = type("Session", (), {"workspace": ws})()

            text = await agent._send_task_status("sid-task", session, VALID_TASK_ID)

        self.assertIn("Stop reason: tool_call_cap", text)
        self.assertEqual(agent._conn.updates[0][0], "sid-task")


if __name__ == "__main__":
    unittest.main()
