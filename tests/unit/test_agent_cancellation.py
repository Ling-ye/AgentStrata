"""Cooperative cancellation behavior and its intentional blocking boundaries."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.prompt_plan_fixture import prompt_plan

from chatcopilot.agent.backends import BackendAgentSession
from chatcopilot.agent.backends.codex import CodexAgentBackend
from chatcopilot.agent.backends.inprocess import InProcessAgentBackend
from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.agent import (
    AgentTask,
    FinalText,
    TextDelta,
    ToolFinished,
)
from chatcopilot.contracts.agent_backend import BackendOpenRequest
from chatcopilot.contracts.cancellation import (
    CancellationProbe,
    CancellationRequested,
    CancellationToken,
)
from chatcopilot.contracts.tools import (
    ToolContext,
    ToolDef,
    ToolResult,
    build_openai_schema,
    object_schema,
)
from chatcopilot.core.llm_client import ChatResult, LLMClient
from chatcopilot.external_tools.codex_cli.process_runner import run_codex_process


class _FakeLLM:
    model = "fake-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0) if self.results else ChatResult(content="")


def _tool_call(name: str) -> dict:
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({})},
    }


def _make_session(llm, tools: list[ToolDef] | None = None) -> AgentSession:
    selected = list(tools or [])
    return AgentSession(
        session_id="cancel-test",
        llm=llm,
        executor=ToolExecutor(tools=selected),
        tools_schema=[build_openai_schema(tool) for tool in selected],
        prompt_plan=prompt_plan("system"),
    )


class CancellationContractTests(unittest.TestCase):
    def test_token_is_a_thread_safe_one_way_probe(self) -> None:
        token = CancellationToken()
        self.assertIsInstance(token, CancellationProbe)
        workers = [threading.Thread(target=token.cancel) for _ in range(8)]

        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)

        self.assertTrue(token.is_cancelled)
        with self.assertRaises(CancellationRequested) as caught:
            token.raise_if_cancelled()
        self.assertEqual(caught.exception.code, "cancelled")
        token.cancel()
        with self.assertRaises(CancellationRequested):
            token.raise_if_cancelled()

    def test_precancelled_native_turn_returns_cancelled_without_side_effects(self) -> None:
        llm = _FakeLLM([ChatResult(content="must not run")])
        session = _make_session(llm)
        before = session.snapshot_messages()
        token = CancellationToken()
        token.cancel()

        result = session.run_task(
            AgentTask("stop"),
            on_event=lambda _event: None,
            cancellation=token,
        )

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(result.final_text, "")
        self.assertEqual(llm.calls, [])
        self.assertEqual(session.snapshot_messages(), before)

    def test_stream_delta_checkpoint_stops_before_next_delta(self) -> None:
        token = CancellationToken()

        class DeltaLLM:
            model = "delta-model"

            def chat(self, **kwargs):
                callback = kwargs["on_content_delta"]
                callback("first")
                token.cancel()
                callback("second")
                return ChatResult(content="firstsecond")

        events: list[object] = []
        result = _make_session(DeltaLLM()).run_task(
            AgentTask("stream"),
            on_event=events.append,
            cancellation=token,
        )

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(
            [event.text for event in events if isinstance(event, TextDelta)],
            ["first"],
        )
        self.assertFalse(any(isinstance(event, FinalText) for event in events))

    def test_llm_client_checks_every_raw_stream_chunk_and_closes_stream(self) -> None:
        token = CancellationToken()

        class Stream:
            def __init__(self) -> None:
                self.closed = False

            def __iter__(self):
                yield SimpleNamespace(usage=None, choices=[])
                token.cancel()
                yield SimpleNamespace(usage=None, choices=[])

            def close(self) -> None:
                self.closed = True

        stream = Stream()
        with self.assertRaises(CancellationRequested):
            LLMClient._consume_stream(
                stream,
                None,
                None,
                cancellation=token,
            )
        self.assertTrue(stream.closed)

    def test_nonstream_llm_is_not_preempted_but_result_is_cancelled_after_return(self) -> None:
        started = threading.Event()
        release = threading.Event()
        token = CancellationToken()

        class BlockingLLM:
            model = "blocking-model"

            def chat(self, **_kwargs):
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("test did not release blocking LLM")
                return ChatResult(content="late answer")

        session = _make_session(BlockingLLM())
        session.stream_first_turn = False
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                session.run_task,
                AgentTask("blocking request"),
                on_event=lambda _event: None,
                cancellation=token,
            )
            self.assertTrue(started.wait(timeout=1))
            token.cancel()
            self.assertFalse(future.done())
            release.set()
            result = future.result(timeout=1)

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(result.final_text, "")

    def test_blocking_tool_is_not_preempted_and_receipt_precedes_cancelled_result(self) -> None:
        started = threading.Event()
        release = threading.Event()
        token = CancellationToken()

        def handler(_args: Mapping[str, Any], _context: ToolContext) -> ToolResult:
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release blocking tool")
            return ToolResult(
                ok=True,
                summary="tool committed",
                data={"committed": True},
            )

        tool = ToolDef(
            name="blocking_tool",
            summary="Block until the test releases it.",
            input_schema=object_schema(),
            output_schema=object_schema(
                {"committed": {"type": "boolean"}},
                required=("committed",),
            ),
            handler=handler,
        )
        llm = _FakeLLM([ChatResult(tool_calls=[_tool_call(tool.name)])])
        session = _make_session(llm, [tool])
        events: list[object] = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                session.run_task,
                AgentTask("run tool"),
                on_event=events.append,
                cancellation=token,
            )
            self.assertTrue(started.wait(timeout=1))
            token.cancel()
            self.assertFalse(future.done())
            release.set()
            result = future.result(timeout=1)

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(len(llm.calls), 1)
        finished = [event for event in events if isinstance(event, ToolFinished)]
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0].ok)
        self.assertEqual(finished[0].summary, "tool committed")

    def test_inprocess_backend_propagates_probe_to_native_session(self) -> None:
        llm = _FakeLLM([ChatResult(content="must not run")])
        native = _make_session(llm)
        backend = InProcessAgentBackend("native", tool_names=set())
        ref = backend.open_session(
            BackendOpenRequest(
                session_id="sid",
                prompt_plan=prompt_plan("system"),
                options={"session_factory": lambda: native},
            )
        )
        session = BackendAgentSession(backend, ref)
        token = CancellationToken()
        token.cancel()

        result = session.run_task(
            AgentTask("stop"),
            on_event=lambda _event: None,
            cancellation=token,
        )

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(llm.calls, [])

    def test_precancelled_langgraph_turn_does_not_require_graph_runtime(self) -> None:
        llm = _FakeLLM([ChatResult(content="must not run")])
        session = LangGraphAgentSession(
            session_id="langgraph-cancel",
            llm=llm,  # type: ignore[arg-type]
            executor=ToolExecutor(tools=[]),
            tools_schema=[],
            prompt_plan=prompt_plan("system"),
        )
        token = CancellationToken()
        token.cancel()

        result = session.run_task(
            AgentTask("stop"),
            on_event=lambda _event: None,
            cancellation=token,
        )

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(llm.calls, [])


class CodexCancellationTests(unittest.TestCase):
    def test_codex_poll_cancellation_reaches_run_task_as_cancelled(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            backend = CodexAgentBackend(
                tool_names=set(),
                runtime_config=SimpleNamespace(routing=routing),
            )
            ref = backend.open_session(
                BackendOpenRequest(
                    session_id="codex-cancel",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root,
                        "backend_state_root": root / "state",
                    },
                )
            )
            session = BackendAgentSession(backend, ref)
            token = CancellationToken()

            @contextmanager
            def fake_credential_lease(*_args, **_kwargs):
                yield SimpleNamespace(generation=0)

            def cancel_from_poll(*_args, **kwargs):
                token.cancel()
                kwargs["on_poll"]()
                raise AssertionError("cancelled poll unexpectedly returned")

            events: list[object] = []
            try:
                with (
                    mock.patch.object(
                        backend,
                        "_bot_credential_root",
                        return_value=root,
                    ),
                    mock.patch(
                        "chatcopilot.agent.backends.codex.credential_lease",
                        side_effect=fake_credential_lease,
                    ),
                    mock.patch(
                        "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                        return_value="/usr/bin/codex",
                    ),
                    mock.patch(
                        "chatcopilot.agent.backends.codex.build_codex_subprocess_env",
                        return_value={},
                    ),
                    mock.patch(
                        "chatcopilot.agent.backends.codex.run_codex_process",
                        side_effect=cancel_from_poll,
                    ),
                ):
                    result = session.run_task(
                        AgentTask("cancel codex"),
                        on_event=events.append,
                        cancellation=token,
                    )
            finally:
                session.close()

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(result.final_text, "")
        self.assertFalse(any(isinstance(event, FinalText) for event in events))

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_poll_cancellation_kills_running_codex_process_group(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path = root / "process.pid"
            token = CancellationToken()

            def cancel_after_start() -> None:
                deadline = time.monotonic() + 2
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if pid_path.exists():
                    token.cancel()

            canceller = threading.Thread(target=cancel_after_start)
            canceller.start()
            started = time.monotonic()
            with self.assertRaises(CancellationRequested):
                run_codex_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,pathlib,subprocess,sys,time; "
                            "child=subprocess.Popen([sys.executable,'-c',"
                            "'import time; time.sleep(30)']); "
                            "pathlib.Path(sys.argv[1]).write_text("
                            "f'{os.getpid()},{child.pid}'); "
                            "time.sleep(30)"
                        ),
                        str(pid_path),
                    ],
                    cwd=root,
                    prompt="",
                    timeout_seconds=9,
                    env=dict(os.environ),
                    on_stdout_line=lambda _line: None,
                    on_poll=token.raise_if_cancelled,
                )
            canceller.join(timeout=1)

            self.assertLess(time.monotonic() - started, 2)
            parent_pid, child_pid = (
                int(value) for value in pid_path.read_text(encoding="ascii").split(",")
            )
            self.assertFalse(Path(f"/proc/{parent_pid}").exists())
            child_stat = Path(f"/proc/{child_pid}/stat")
            reap_deadline = time.monotonic() + 1
            while child_stat.exists() and time.monotonic() < reap_deadline:
                fields = child_stat.read_text(encoding="ascii", errors="replace").split()
                if len(fields) > 2 and fields[2] == "Z":
                    break
                time.sleep(0.01)
            if child_stat.exists():
                fields = child_stat.read_text(encoding="ascii", errors="replace").split()
                self.assertGreater(len(fields), 2)
                self.assertEqual(fields[2], "Z")


if __name__ == "__main__":
    unittest.main()
