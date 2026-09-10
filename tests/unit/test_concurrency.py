"""Regression tests for bot concurrency guards."""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from chatcopilot.core.concurrency import FileTokenLimiter
from chatcopilot.middleware.runtime.jobs import FileQueueSlot
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.middleware.acp.server import AcpChatAgent
from chatcopilot.contracts.tools import (
    EXECUTION_SYNC,
    EXECUTION_USER_SERIAL_BACKGROUND,
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


def _process_competitor(root, start, pair, active, maximum, guard):
    limiter = FileTokenLimiter("shared", 2, root=Path(root), queue_timeout=10)
    assert start.wait(10)
    with limiter.slot():
        with guard:
            active.value += 1
            maximum.value = max(maximum.value, active.value)
        pair.wait(10)
        time.sleep(0.02)
        with guard:
            active.value -= 1


def _process_holder(root, ready, release, allow_create):
    limiter = FileTokenLimiter("shared", 1, root=Path(root), queue_timeout=10)
    if allow_create is not None:
        original = limiter._create_token

        def delayed_create():
            ready.send("creating")
            assert allow_create.wait(10)
            return original()

        limiter._create_token = delayed_create
    token = limiter.acquire()
    try:
        ready.send(str(token))
        assert release.wait(10)
    finally:
        limiter.release(token)


def _stop_processes(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(5)
        assert not process.is_alive()


def test_late_publication_with_earlier_timestamp_cannot_displace_holders(tmp_path):
    limiter = FileTokenLimiter("test", 2, root=tmp_path, queue_timeout=0.05)
    tokens = [limiter.acquire(), limiter.acquire()]
    try:
        # An earlier filename published late is the original race's decisive
        # input. It must never outrank either already admitted holder.
        with mock.patch("chatcopilot.core.concurrency.time.monotonic_ns", return_value=0):
            with pytest.raises(TimeoutError):
                tokens.append(limiter.acquire())
        assert set(limiter.root.glob("*.token")) == set(tokens)
    finally:
        for token in tokens:
            limiter.release(token)


@pytest.mark.parametrize("shared_instance", [True, False])
def test_thread_contenders_preserve_capacity_and_parallelism(tmp_path, shared_instance):
    shared = FileTokenLimiter("shared", 2, root=tmp_path, queue_timeout=10)
    start, pair = threading.Barrier(7), threading.Barrier(2)
    active = maximum = 0
    guard = threading.Lock()
    errors = []

    def worker():
        nonlocal active, maximum
        limiter = shared if shared_instance else FileTokenLimiter("shared", 2, root=tmp_path, queue_timeout=10)
        try:
            start.wait(10)
            with limiter.slot():
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                pair.wait(10)
                time.sleep(0.02)
                with guard:
                    active -= 1
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    start.wait(10)
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert maximum == 2 and active == 0
    assert not list(shared.root.glob("*.token"))


def test_independent_processes_share_capacity(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, pair, guard = ctx.Event(), ctx.Barrier(2), ctx.Lock()
    active, maximum = ctx.Value("i", 0), ctx.Value("i", 0)
    processes = [ctx.Process(target=_process_competitor, args=(str(tmp_path), start, pair, active, maximum, guard)) for _ in range(6)]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(15)
        assert [process.exitcode for process in processes] == [0] * len(processes)
        assert maximum.value == 2 and active.value == 0
        assert not list((tmp_path / "shared").glob("*.token"))
    finally:
        _stop_processes(processes)


def test_queue_timeout_includes_wait_for_admission_transaction(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    release, allow_create = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_process_holder, args=(str(tmp_path), child, release, allow_create))
    process.start()
    try:
        assert parent.poll(10) and parent.recv() == "creating"
        contender = FileTokenLimiter("shared", 1, root=tmp_path, queue_timeout=0.05)
        with pytest.raises(TimeoutError):
            contender.acquire()
        assert not list(contender.root.glob("*.token"))
        allow_create.set()
        assert parent.poll(10)
        assert Path(parent.recv()).is_file()
        release.set()
        process.join(10)
        assert process.exitcode == 0
    finally:
        release.set()
        allow_create.set()
        _stop_processes([process])
        parent.close()
        child.close()


def test_ttl_preserves_live_holder_and_recovers_crashed_process(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    release = ctx.Event()
    process = ctx.Process(target=_process_holder, args=(str(tmp_path), child, release, None))
    process.start()
    try:
        assert parent.poll(10)
        token = Path(parent.recv())
        os.utime(token, (0, 0))
        contender = FileTokenLimiter("shared", 1, root=tmp_path, queue_timeout=0.05, stale_seconds=0.1)
        with pytest.raises(TimeoutError):
            contender.acquire()
        assert token.is_file()
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        with contender.slot():
            assert not token.exists()
            assert len(list(contender.root.glob("*.token"))) == 1
        assert not list(contender.root.glob("*.token"))
    finally:
        if process.is_alive():
            release.set()
        _stop_processes([process])
        parent.close()
        child.close()


def test_release_is_idempotent_and_exception_safe(tmp_path):
    limiter = FileTokenLimiter("test", 1, root=tmp_path, queue_timeout=0.05)
    first = limiter.acquire()
    limiter.release(first)
    second = limiter.acquire()
    try:
        limiter.release(first)
        with pytest.raises(TimeoutError):
            limiter.acquire()
        assert second.is_file()
    finally:
        limiter.release(second)
    with pytest.raises(ValueError, match="body failed"):
        with limiter.slot():
            raise ValueError("body failed")
    with FileTokenLimiter("test", 1, root=tmp_path, queue_timeout=0.05).slot():
        assert len(list(limiter.root.glob("*.token"))) == 1


def test_failed_token_creation_releases_descriptor_and_admission_lock(tmp_path, monkeypatch):
    from chatcopilot.core import concurrency

    limiter = FileTokenLimiter("test", 1, root=tmp_path, queue_timeout=0.05)
    descriptors = []

    def fail_write(fd, _data):
        descriptors.append(fd)
        raise OSError("write failed")

    with monkeypatch.context() as patcher:
        patcher.setattr(concurrency.os, "write", fail_write)
        with pytest.raises(OSError, match="write failed"):
            limiter.acquire()
    assert not list(limiter.root.glob("*.token"))
    assert not limiter._held_tokens
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    with FileTokenLimiter("test", 1, root=tmp_path, queue_timeout=0.05).slot():
        pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork ownership")
def test_forked_child_cannot_release_parent_token(tmp_path):
    limiter = FileTokenLimiter("test", 1, root=tmp_path, queue_timeout=0.05, stale_seconds=0.1)
    token = limiter.acquire()
    try:
        pid = os.fork()
        if pid == 0:
            try:
                limiter.release(token)
            finally:
                os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        os.utime(token, (0, 0))
        with pytest.raises(TimeoutError):
            FileTokenLimiter("test", 1, root=tmp_path, queue_timeout=0.05, stale_seconds=0.1).acquire()
        assert token.is_file()
    finally:
        limiter.release(token)


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
        def handler(_args: dict, _context: ToolContext) -> ToolResult:
            return ToolResult(ok=True, summary="ok")

        tool = ToolDef(
            name="light",
            summary="light test tool",
            input_schema=object_schema(),
            output_schema=object_schema(),
            handler=handler,
        )

        self.assertEqual(tool.weight, "light")
        self.assertEqual(tool.execution_policy, EXECUTION_SYNC)

    def test_background_policy_submits_without_running_handler(self) -> None:
        called = False

        def handler(_args: dict, _context: ToolContext) -> ToolResult:
            nonlocal called
            called = True
            return ToolResult(ok=True, summary="ran")

        tool = ToolDef(
            name="bg",
            summary="background test tool",
            input_schema=object_schema(
                {"x": {"type": "integer"}},
                required=("x",),
            ),
            output_schema=object_schema(),
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

        result = ToolExecutor(
            caller_role_hint="owner", tools=[tool], background_submitter=submitter
        ).execute("bg", {"x": 1})

        self.assertFalse(called)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "queued")

    def test_background_worker_executes_handler_directly(self) -> None:
        called = False

        def handler(_args: dict, _context: ToolContext) -> ToolResult:
            nonlocal called
            called = True
            return ToolResult(ok=True, summary="ran")

        tool = ToolDef(
            name="bg",
            summary="background test tool",
            input_schema=object_schema(),
            output_schema=object_schema(),
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
            result = ToolExecutor(
                caller_role_hint="owner", tools=[tool], background_submitter=submitter
            ).execute("bg", {})

        self.assertTrue(called)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "ran")

    def test_background_submitter_rejects_legacy_tuple_result(self) -> None:
        called = False

        def handler(_args: dict, _context: ToolContext) -> ToolResult:
            nonlocal called
            called = True
            return ToolResult(ok=True, summary="ran")

        tool = ToolDef(
            name="bg_invalid_result",
            summary="Background result contract test tool.",
            input_schema=object_schema(),
            output_schema=object_schema(),
            handler=handler,
            execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
        )

        def legacy_submitter(_tool: ToolDef, _args: dict):
            return "queued", [], None

        result = ToolExecutor(
            caller_role_hint="owner",
            tools=[tool],
            background_submitter=legacy_submitter,  # type: ignore[arg-type]
        ).execute(tool.name, {})

        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.ok)
        self.assertFalse(called)

    def test_heavy_tool_uses_global_limiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active = 0
            max_seen = 0
            guard = threading.Lock()

            def handler(_args: dict, _context: ToolContext) -> ToolResult:
                nonlocal active, max_seen
                with guard:
                    active += 1
                    max_seen = max(max_seen, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return ToolResult(ok=True, summary="ok")

            tool = ToolDef(
                name="heavy",
                summary="heavy test tool",
                input_schema=object_schema(),
                output_schema=object_schema(),
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
                executor = ToolExecutor(caller_role_hint="owner", tools=[tool])
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
