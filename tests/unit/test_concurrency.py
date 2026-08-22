"""Regression tests for bot concurrency guards."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chatcopilot.core.concurrency import FileTokenLimiter
from chatcopilot.middleware.runtime.jobs import FileQueueSlot
from chatcopilot.agent.tools.executor import ToolExecutor, ToolResult
from chatcopilot.middleware.acp.server import AcpChatAgent
from chatcopilot.contracts.tools import (
    EXECUTION_SYNC,
    EXECUTION_USER_SERIAL_BACKGROUND,
    ToolDef,
)


class FileTokenLimiterTests(unittest.TestCase):
    def test_limiter_never_exceeds_configured_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            limiter = FileTokenLimiter(
                "test",
                2,
                root=Path(tmp),
                stale_seconds=30,
                poll_interval=0.01,
            )
            active = 0
            max_seen = 0
            guard = threading.Lock()

            def worker() -> None:
                nonlocal active, max_seen
                with limiter.slot():
                    with guard:
                        active += 1
                        max_seen = max(max_seen, active)
                    time.sleep(0.05)
                    with guard:
                        active -= 1

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertLessEqual(max_seen, 2)


class SessionLockTests(unittest.TestCase):
    def test_same_session_lock_serializes_work(self) -> None:
        async def run_case() -> int:
            # 直接走 __new__ + 手动初始化 _session_locks，避免触发 LLM/runtime 装配，
            # 这测的仅是 asyncio 锁的串行语义本身。
            agent = AcpChatAgent.__new__(AcpChatAgent)
            agent._session_locks = {}
            active = 0
            max_seen = 0

            async def worker() -> None:
                nonlocal active, max_seen
                async with agent._session_lock("sid"):
                    active += 1
                    max_seen = max(max_seen, active)
                    await asyncio.sleep(0.01)
                    active -= 1

            await asyncio.gather(worker(), worker(), worker())
            return max_seen

        self.assertEqual(asyncio.run(run_case()), 1)


class ToolExecutorLimiterTests(unittest.TestCase):
    def test_tool_weight_defaults_to_light(self) -> None:
        tool = ToolDef(
            name="light",
            summary="light test tool",
            properties={},
            required=[],
            handler=lambda _args: ("ok", [], None),
        )

        self.assertEqual(tool.weight, "light")
        self.assertEqual(tool.execution_policy, EXECUTION_SYNC)

    def test_background_policy_submits_without_running_handler(self) -> None:
        called = False

        def handler(_args):
            nonlocal called
            called = True
            return "ran", [], None

        tool = ToolDef(
            name="bg",
            summary="background test tool",
            properties={},
            required=[],
            handler=handler,
            execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
        )

        def submitter(submitted_tool, submitted_args):
            self.assertEqual(submitted_tool.name, "bg")
            self.assertEqual(submitted_args, {"x": 1})
            return ToolResult(
                ok=True,
                summary="queued",
                outputs=[],
                console="",
                doc_links=[],
            )

        result = ToolExecutor(tools=[tool], background_submitter=submitter).execute(
            "bg", {"x": 1}
        )

        self.assertFalse(called)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "queued")

    def test_background_worker_executes_handler_directly(self) -> None:
        called = False

        def handler(_args):
            nonlocal called
            called = True
            return "ran", [], None

        tool = ToolDef(
            name="bg",
            summary="background test tool",
            properties={},
            required=[],
            handler=handler,
            execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
        )

        def submitter(_tool, _args):
            raise AssertionError("worker must not resubmit background jobs")

        with mock.patch.dict(
            "os.environ",
            {"CHATCOPILOT_BACKGROUND_WORKER": "1"},
            clear=False,
        ):
            result = ToolExecutor(tools=[tool], background_submitter=submitter).execute(
                "bg", {}
            )

        self.assertTrue(called)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "ran")

    def test_heavy_tool_uses_global_limiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active = 0
            max_seen = 0
            guard = threading.Lock()

            def handler(_args):
                nonlocal active, max_seen
                with guard:
                    active += 1
                    max_seen = max(max_seen, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return "ok", [], None

            tool = ToolDef(
                name="heavy",
                summary="heavy test tool",
                properties={},
                required=[],
                handler=handler,
                weight="heavy",
            )

            with mock.patch.dict(
                "os.environ",
                {
                    "CHATCOPILOT_LIMIT_DIR": tmp,
                    "CHATCOPILOT_HEAVY_TOOL_CONCURRENCY": "1",
                },
                clear=False,
            ):
                executor = ToolExecutor(tools=[tool])
                threads = [
                    threading.Thread(target=lambda: executor.execute("heavy", {}))
                    for _ in range(3)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

        self.assertEqual(max_seen, 1)


class FileQueueSlotTests(unittest.TestCase):
    def test_queue_slot_serializes_same_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active = 0
            max_seen = 0
            guard = threading.Lock()

            def worker(idx: int) -> None:
                nonlocal active, max_seen
                with mock.patch.dict(
                    "os.environ",
                    {
                        "CHATCOPILOT_LIMIT_DIR": tmp,
                        "CHATCOPILOT_JOB_POLL_INTERVAL": "0.01",
                    },
                    clear=False,
                ):
                    with FileQueueSlot("same-user", f"job-{idx}", capacity=1):
                        with guard:
                            active += 1
                            max_seen = max(max_seen, active)
                        time.sleep(0.03)
                        with guard:
                            active -= 1

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(max_seen, 1)

    def test_queue_slot_allows_different_user_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active = 0
            max_seen = 0
            guard = threading.Lock()

            def worker(queue_name: str) -> None:
                nonlocal active, max_seen
                with mock.patch.dict(
                    "os.environ",
                    {
                        "CHATCOPILOT_LIMIT_DIR": tmp,
                        "CHATCOPILOT_JOB_POLL_INTERVAL": "0.01",
                    },
                    clear=False,
                ):
                    with FileQueueSlot(queue_name, queue_name, capacity=1):
                        with guard:
                            active += 1
                            max_seen = max(max_seen, active)
                        time.sleep(0.05)
                        with guard:
                            active -= 1

            threads = [
                threading.Thread(target=worker, args=(f"user-{i}",))
                for i in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertGreater(max_seen, 1)


if __name__ == "__main__":
    unittest.main()
