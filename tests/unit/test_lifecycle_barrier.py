from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from chatcopilot.agent.protocol import DeferredLifecycleIntent
from chatcopilot.middleware.acp.lifecycle_barrier import LifecycleBarrierExecutor


class _Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.chat_kind = "p2p"
        self.chat_id = "chat-1"
        self.user_id = "user-1"
        self.user_name = "tester"


class LifecycleBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_self_update_uses_explicit_workspace_payload(self) -> None:
        intent = DeferredLifecycleIntent(
            name="finalize_self_update",
            arguments={"reason": "修复测试"},
            source="main",
        )
        with TemporaryDirectory() as tmp:
            workspace = _Workspace(Path(tmp))
            with mock.patch(
                "chatcopilot.middleware.acp.lifecycle_barrier.execute_finalize_self_update_from_workspace",
                return_value=(
                    "已提交自更新任务。\njob_id: job_20260708_123456_abcdef12",
                    [str(Path(tmp) / "jobs" / "job_20260708_123456_abcdef12")],
                    None,
                ),
            ) as execute:
                result = await LifecycleBarrierExecutor().execute(
                    (intent,),
                    final_text_delivered=True,
                    workspace=workspace,
                    session_id="sid-1",
                )

        self.assertEqual(result.status, "started")
        self.assertEqual(result.job_id, "job_20260708_123456_abcdef12")
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], {"reason": "修复测试"})
        self.assertEqual(execute.call_args.kwargs["session_id"], "sid-1")
        self.assertEqual(execute.call_args.kwargs["workspace_payload"]["root"], str(workspace.root))

    async def test_finalize_self_update_requires_workspace(self) -> None:
        intent = DeferredLifecycleIntent(
            name="finalize_self_update",
            arguments={"reason": "修复测试"},
            source="main",
        )

        result = await LifecycleBarrierExecutor().execute(
            (intent,),
            final_text_delivered=True,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("workspace is required", result.error)

if __name__ == "__main__":
    unittest.main()
