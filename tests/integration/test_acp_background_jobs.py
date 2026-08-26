"""Regression tests for Feishu ACP background job notifications."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from chatcopilot.platforms.feishu import notifier as feishu_notifier
from chatcopilot.middleware.access_control import AssistantMode
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.middleware.acp import server as acp_server
from chatcopilot.middleware.acp.server import AcpChatAgent


class _FakeConn:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


class _FakeSession:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.is_workspace_materialized = False
        self.debug_mode = False
        self.assistant_mode = AssistantMode.PERFORMANCE

    def materialize_workspace(self) -> bool:
        if self.is_workspace_materialized:
            return False
        self.workspace = self.workspace.ensure()
        self.is_workspace_materialized = True
        return True

    def message_count(self) -> int:
        return 1


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_finished_job(ws: Workspace, job_id: str = "job_20260515_174720_1a9a4fb8") -> Path:
    job_dir = ws.root / "jobs" / job_id
    _write_json(
        job_dir / "request.json",
        {
            "job_id": job_id,
            "tool_name": "long_running_export",
            "execution_policy": "global_serial_background",
            "queue_name": "datasource_global",
            "workspace": {
                "root": str(ws.root),
                "chat_kind": ws.chat_kind,
                "chat_id": ws.chat_id,
                "user_id": ws.user_id,
                "user_name": ws.user_name,
            },
            "notify": {
                "session_id": "sid",
                "chat_kind": ws.chat_kind,
                "chat_id": ws.chat_id,
                "user_id": ws.user_id,
                "user_name": ws.user_name,
            },
        },
    )
    _write_json(job_dir / "status.json", {"status": "succeeded", "message": "ok"})
    _write_json(
        job_dir / "result.json",
        {
            "job_id": job_id,
            "tool_name": "long_running_export",
            "ok": True,
            "summary": "已同步 1 个数据源到目标飞书性能数据仓库表",
            "outputs": [],
            "started_at": 1,
            "finished_at": 3,
        },
    )
    return job_dir


class BackgroundJobNotificationTests(unittest.TestCase):
    def test_job_status_prompt_reads_result_without_llm(self) -> None:
        async def run_case() -> tuple[list[tuple[str, Any]], AsyncMock]:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                    ).ensure()
                    _seed_finished_job(ws)
                    session = _FakeSession(ws)
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    ensure_agent_session = AsyncMock(
                        side_effect=AssertionError(
                            "job status shortcut must not materialize an Agent session"
                        )
                    )
                    agent._ensure_agent_session = ensure_agent_session

                    await agent._prompt_locked(
                        [{"type": "text", "text": "job_20260515_174720_1a9a4fb8 处理完了吗？"}],
                        "sid",
                        "mid",
                    )

                    return agent._conn.updates, ensure_agent_session
            finally:
                acp_server.update_agent_message_text = original_update

        updates, ensure_agent_session = asyncio.run(run_case())
        ensure_agent_session.assert_not_awaited()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "sid")
        self.assertIn("后台任务已完成：long_running_export", updates[0][1])
        self.assertIn("已同步 1 个数据源", updates[0][1])

    def test_unnotified_finished_job_is_delivered_by_feishu_openapi(self) -> None:
        async def run_case() -> tuple[list[tuple[str, Any]], dict[str, Any], list[tuple[str, str]]]:
            calls: list[tuple[str, str]] = []
            original_send = acp_server.feishu_notifier.send_text_to_workspace

            def fake_send(ws: Workspace, text: str) -> Any:
                calls.append((ws.user_id or "", text))
                return SimpleNamespace(
                    receive_id_type="open_id",
                    receive_id=ws.user_id,
                    message_id="om_delivered",
                )

            acp_server.feishu_notifier.send_text_to_workspace = fake_send
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                    ).ensure()
                    job_dir = _seed_finished_job(ws)
                    session = _FakeSession(ws)
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._conn = _FakeConn()

                    await agent._send_unnotified_completed_jobs("sid", session)  # type: ignore[arg-type]

                    notification = json.loads((job_dir / "notification.json").read_text(encoding="utf-8"))
                    return agent._conn.updates, notification, calls
            finally:
                acp_server.feishu_notifier.send_text_to_workspace = original_send

        updates, notification, calls = asyncio.run(run_case())
        self.assertEqual(len(updates), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ou_test")
        self.assertIn("后台任务已完成", calls[0][1])
        self.assertEqual(notification["delivery"], "delivered")
        self.assertEqual(notification["channel"], "feishu_openapi")
        self.assertEqual(notification["attempts"], 1)
        self.assertEqual(notification["session_id"], "sid")
        self.assertEqual(notification["receive_id_type"], "open_id")
        self.assertEqual(notification["receive_id"], "ou_test")
        self.assertEqual(notification["message_id"], "om_delivered")

    def test_unnotified_finished_job_sends_output_files(self) -> None:
        async def run_case() -> tuple[list[tuple[str, str]], list[list[str]], dict[str, Any]]:
            text_calls: list[tuple[str, str]] = []
            file_calls: list[list[str]] = []
            original_text_send = acp_server.feishu_notifier.send_text_to_workspace
            original_file_send = acp_server.feishu_sender.send_via_cc_connect

            def fake_text_send(ws: Workspace, text: str) -> Any:
                text_calls.append((ws.user_id or "", text))
                return SimpleNamespace(
                    receive_id_type="open_id",
                    receive_id=ws.user_id,
                    message_id="om_delivered",
                )

            def fake_file_send(files: Any, message: str = "", timeout: Any = None) -> str:
                file_calls.append([str(path) for path in files])
                return "sent"

            acp_server.feishu_notifier.send_text_to_workspace = fake_text_send
            acp_server.feishu_sender.send_via_cc_connect = fake_file_send
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                    ).ensure()
                    output = ws.results / "diff.xlsx"
                    output.write_bytes(b"xlsx")
                    job_dir = _seed_finished_job(ws)
                    result_path = job_dir / "result.json"
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["outputs"] = [str(output), str(ws.results)]
                    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

                    session = _FakeSession(ws)
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._conn = _FakeConn()

                    await agent._send_unnotified_completed_jobs("sid", session)  # type: ignore[arg-type]

                    notification = json.loads((job_dir / "notification.json").read_text(encoding="utf-8"))
                    return text_calls, file_calls, notification
            finally:
                acp_server.feishu_notifier.send_text_to_workspace = original_text_send
                acp_server.feishu_sender.send_via_cc_connect = original_file_send

        text_calls, file_calls, notification = asyncio.run(run_case())
        self.assertEqual(len(text_calls), 1)
        self.assertEqual(len(file_calls), 1)
        self.assertEqual(len(file_calls[0]), 1)
        self.assertTrue(file_calls[0][0].endswith("diff.xlsx"))
        self.assertEqual(notification["delivery"], "delivered")

    def test_failed_notification_is_retried_until_delivered(self) -> None:
        async def run_case() -> tuple[dict[str, Any], dict[str, Any]]:
            calls = {"count": 0}
            original_send = acp_server.feishu_notifier.send_text_to_workspace

            def flaky_send(ws: Workspace, text: str) -> Any:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise feishu_notifier.FeishuNotifyError("network down")
                return SimpleNamespace(
                    receive_id_type="open_id",
                    receive_id=ws.user_id,
                    message_id="om_retry",
                )

            acp_server.feishu_notifier.send_text_to_workspace = flaky_send
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                    ).ensure()
                    job_dir = _seed_finished_job(ws)
                    session = _FakeSession(ws)
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._conn = _FakeConn()

                    with self.assertLogs("chatcopilot.middleware.acp.job_dispatch", level="ERROR"):
                        await agent._send_unnotified_completed_jobs("sid", session)  # type: ignore[arg-type]
                    first = json.loads((job_dir / "notification.json").read_text(encoding="utf-8"))
                    await agent._send_unnotified_completed_jobs("sid", session)  # type: ignore[arg-type]
                    second = json.loads((job_dir / "notification.json").read_text(encoding="utf-8"))
                    return first, second
            finally:
                acp_server.feishu_notifier.send_text_to_workspace = original_send

        first, second = asyncio.run(run_case())
        self.assertEqual(first["delivery"], "failed")
        self.assertEqual(first["attempts"], 1)
        self.assertIn("network down", first["last_error"])
        self.assertEqual(second["delivery"], "delivered")
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["message_id"], "om_retry")

    def test_delivery_target_prefers_open_id_for_p2p(self) -> None:
        ws = Workspace(root=Path("unused"), chat_kind="p2p", chat_id=None, user_id="ou_test")

        target = feishu_notifier.resolve_delivery_target(ws)

        self.assertEqual(target.receive_id_type, "open_id")
        self.assertEqual(target.receive_id, "ou_test")

    def test_delivery_target_uses_chat_id_for_group(self) -> None:
        ws = Workspace(root=Path("unused"), chat_kind="group", chat_id="oc_group", user_id="ou_test")

        target = feishu_notifier.resolve_delivery_target(ws)

        self.assertEqual(target.receive_id_type, "chat_id")
        self.assertEqual(target.receive_id, "oc_group")


if __name__ == "__main__":
    unittest.main()
