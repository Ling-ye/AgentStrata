from __future__ import annotations

from tests.prompt_plan_fixture import prompt_input, prompt_plan

import json
import os
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from chatcopilot.agent.backends.codex import CodexAgentBackend
from chatcopilot.agent.backends.codex_events import CodexJsonlProjector
from chatcopilot.agent.backends.registry import backend_ids, build_backend
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.trace import current_trace
from chatcopilot.agent.tools.executor import ToolExecutor, ToolResult
from chatcopilot.botspec.backend_state import prepare_backend_deployment
from chatcopilot.contracts.agent import (
    AgentTask,
    ContextSnapshotPrepared,
    FinalText,
    LlmCallFinished,
    LlmCallStarted,
    ResourceRef,
    SpanFinished,
    SpanStarted,
    ToolFinished,
    ToolStarted,
    TurnError,
)
from chatcopilot.contracts.agent_backend import (
    BackendCapabilityError,
    BackendCapabilities,
    BackendOpenRequest,
    BackendSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_NATIVE_RESUME,
    CodexMainSessionPolicy,
)
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.contracts.model_selection import (
    CodeModelProfile,
    CodeModelSelection,
)
from chatcopilot.core.model_selection import CODE_MODEL_SELECTION_METADATA_KEY
from chatcopilot.core.config import ChatConfig
from chatcopilot.external_tools.shared.tool_spec import ToolDef
from chatcopilot.external_tools.codex_cli.credentials import (
    CredentialError,
    install_login_credential,
)
from chatcopilot.middleware.acp.turn_pipeline import (
    CallbackTurnHandler,
    OrderedTurnPipeline,
    TURN_STAGE_ORDER,
    TurnContext,
    TurnOutcome,
)
from chatcopilot.agent.backends.session_relay import (
    SessionToolRelay,
    call_session_relay,
)


def _dynamic_tool(calls: list[str] | None = None) -> ToolDef:
    def handler(args):
        value = str(args.get("value") or "")
        if calls is not None:
            calls.append(value)
        return f"dynamic:{value}", [], None

    return ToolDef(
        name="dynamic_echo",
        summary="Echo through the live session executor.",
        properties={"value": {"type": "string"}},
        required=["value"],
        handler=handler,
    )


def _main_auth_root(root: Path, *, token: str = "test") -> Path:
    auth_root = root / "codex-auth"
    staging = root / f"codex-auth-staging-{token}"
    staging.mkdir(mode=0o700)
    auth = staging / "auth.json"
    auth.write_text(json.dumps(_codex_auth_payload(token)), encoding="utf-8")
    auth.chmod(0o600)
    install_login_credential(auth_root, "main", staging)
    return auth_root


def _codex_auth_payload(token: str) -> dict[str, object]:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": f"id-{token}",
            "access_token": f"access-{token}",
            "refresh_token": token,
            "account_id": "test-account",
        },
        "last_refresh": "2026-07-28T00:00:00Z",
    }


class BackendRegistryTests(TestCase):
    def test_three_main_backends_are_code_registered(self) -> None:
        self.assertEqual(backend_ids(), {"native", "langgraph", "codex"})

    def test_unknown_backend_fails_without_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported agent backend"):
            build_backend("other", tool_names=set())

    def test_missing_capability_is_deterministic_and_does_not_fallback(self) -> None:
        backend = build_backend("native", tool_names=set())
        with self.assertRaises(BackendCapabilityError) as caught:
            backend.open_session(
                BackendOpenRequest(
                    session_id="sid",
                    prompt_plan=prompt_plan("system"),
                    required_capabilities=frozenset({CAPABILITY_NATIVE_RESUME}),
                )
            )
        self.assertEqual(caught.exception.error_code, "backend_capability_missing")
        self.assertIn("agents.backend", str(caught.exception))


class CodexBackendResumeTests(TestCase):
    def test_main_credential_root_rejects_default_personal_home(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_CODEX_BOT_HOME": str(Path("~").expanduser() / ".codex")},
                clear=True,
            ),
            self.assertRaisesRegex(CredentialError, "auth_root_personal_forbidden"),
        ):
            CodexAgentBackend._bot_credential_root()

    def test_main_credential_root_rejects_personal_home_descendant(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_CODEX_BOT_HOME": str(Path("~").expanduser() / ".codex" / "bot-auth")},
                clear=True,
            ),
            self.assertRaisesRegex(CredentialError, "auth_root_personal_forbidden"),
        ):
            CodexAgentBackend._bot_credential_root()

    def test_codex_native_session_id_is_reused_for_second_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            backend = CodexAgentBackend(
                tool_names={"dynamic_echo", "denied"},
                runtime_config=SimpleNamespace(routing=routing),
                tools=(_dynamic_tool(),),
            )
            ref = backend.open_session(
                BackendOpenRequest(
                    session_id="acp-1",
                    prompt_plan=prompt_plan("system"),
                    allowed_tool_names=frozenset({"dynamic_echo"}),
                    options={
                        "workspace_root": root,
                        "backend_state_root": root / "state",
                        "role_hint": "owner",
                    },
                )
            )
            first = subprocess.CompletedProcess(
                ["codex"],
                0,
                "\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "thread-native-1"}),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "first"},
                            }
                        ),
                    ]
                ),
                "",
            )
            second = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "second"},
                    }
                ),
                "",
            )
            private_resource_path = "/opt/private/agentstrata-secret.dat"
            resource = ResourceRef(
                name="secret.dat",
                path=private_resource_path,
                sha256="a" * 64,
            )
            first_events: list[object] = []
            second_events: list[object] = []
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    side_effect=[first, second],
                ) as run,
            ):
                result1 = backend.stream_turn(
                    ref,
                    AgentTask("one", resources=(resource,)),
                    on_event=first_events.append,
                )
                native_ref = backend.current_session_ref(ref)
                result2 = backend.stream_turn(
                    native_ref,
                    AgentTask("two"),
                    on_event=second_events.append,
                )

            self.assertEqual(result1.final_text, "first")
            self.assertEqual(result2.final_text, "second")
            self.assertEqual(native_ref.value, "thread-native-1")
            resume_command = run.call_args_list[1].args[0]
            resume_index = resume_command.index("resume")
            self.assertEqual(
                resume_command[resume_index:],
                ["resume", "thread-native-1", "-"],
            )
            for option in ("--sandbox", "--cd", "--json"):
                self.assertLess(resume_command.index(option), resume_index)
            first_context = next(
                event
                for event in first_events
                if isinstance(event, ContextSnapshotPrepared)
            )
            second_context = next(
                event
                for event in second_events
                if isinstance(event, ContextSnapshotPrepared)
            )
            for context in (first_context, second_context):
                serialized = json.dumps(
                    list(context.session_messages),
                    ensure_ascii=False,
                )
                self.assertNotIn(private_resource_path, serialized)
                self.assertIn("$RESOURCE_aaaaaaaaaaaa", serialized)
            gateway = json.loads(
                backend.native_session(native_ref).gateway_config.read_text(encoding="utf-8")
            )
            self.assertEqual(gateway["allowed_tools"], ["dynamic_echo"])
            backend.close_session(native_ref)

    def test_session_relay_tool_receipts_are_emitted_as_agent_events(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            handler_threads: list[int] = []

            def traced_handler(args):
                trace = current_trace()
                self.assertIsNotNone(trace)
                assert trace is not None
                self.assertIsNotNone(trace.sink)
                assert trace.sink is not None
                handler_threads.append(threading.get_ident())
                nested_span_id = "span_nested_relay_llm"
                trace.sink(
                    ContextSnapshotPrepared(
                        snapshot_id="ctx_nested_relay",
                        backend="nested-test",
                        model="nested-model",
                        iteration=0,
                        session_messages=(),
                        effective_messages=(),
                        trace_id=trace.trace_id,
                        span_id=nested_span_id,
                        parent_span_id=trace.span_id,
                        depth=trace.depth + 1,
                    )
                )
                trace.sink(
                    LlmCallStarted(
                        model="nested-model",
                        iteration=0,
                        backend="nested-test",
                        trace_id=trace.trace_id,
                        span_id=nested_span_id,
                        parent_span_id=trace.span_id,
                        depth=trace.depth + 1,
                        context_snapshot_id="ctx_nested_relay",
                    )
                )
                value = str(args.get("value") or "")
                return f"dynamic:{value}", [], None

            traced_tool = ToolDef(
                name="dynamic_echo",
                summary="Emit nested trace events through the live relay.",
                properties={"value": {"type": "string"}},
                required=["value"],
                handler=traced_handler,
            )
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            backend = CodexAgentBackend(
                tool_names={"dynamic_echo"},
                runtime_config=SimpleNamespace(routing=routing),
                tools=(traced_tool,),
            )
            ref = backend.open_session(
                BackendOpenRequest(
                    session_id="relay-evidence",
                    prompt_plan=prompt_plan("system"),
                    allowed_tool_names=frozenset({"dynamic_echo"}),
                    options={
                        "workspace_root": root,
                        "backend_state_root": root / "state",
                        "role_hint": "owner",
                    },
                )
            )
            events: list[object] = []
            event_threads: list[int] = []

            def collect_event(event: object) -> None:
                event_threads.append(threading.get_ident())
                events.append(event)

            def run_with_relay(*_args, **kwargs):
                gateway = json.loads(
                    backend.native_session(ref).gateway_config.read_text(encoding="utf-8")
                )
                response = call_session_relay(
                    gateway["relay"],
                    {
                        "action": "call_tool",
                        "name": "dynamic_echo",
                        "arguments": {"value": "bridge-evidence"},
                    },
                )
                self.assertTrue(response["result"]["ok"])
                kwargs["on_poll"]()
                self.assertTrue(any(isinstance(event, ToolStarted) for event in events))
                self.assertTrue(any(isinstance(event, ToolFinished) for event in events))
                return subprocess.CompletedProcess(
                    ["codex"],
                    0,
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "done"},
                        }
                    ),
                    "",
                )

            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    side_effect=run_with_relay,
                ),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("use the tool"),
                    on_event=collect_event,
                )

            started = next(event for event in events if isinstance(event, ToolStarted))
            finished = next(event for event in events if isinstance(event, ToolFinished))
            context = next(
                event for event in events if isinstance(event, ContextSnapshotPrepared)
            )
            llm_started = next(
                event for event in events if isinstance(event, LlmCallStarted)
            )
            llm_finished = next(
                event for event in events if isinstance(event, LlmCallFinished)
            )
            nested_context = next(
                event
                for event in events
                if isinstance(event, ContextSnapshotPrepared)
                and event.backend == "nested-test"
            )
            nested_llm_started = next(
                event
                for event in events
                if isinstance(event, LlmCallStarted)
                and event.backend == "nested-test"
            )
            self.assertEqual(started.name, "dynamic_echo")
            self.assertEqual(started.arguments, {"value": "bridge-evidence"})
            self.assertEqual(started.trace_id, finished.trace_id)
            self.assertEqual(started.trace_id, context.trace_id)
            self.assertEqual(started.trace_id, llm_started.trace_id)
            self.assertEqual(started.parent_span_id, llm_started.span_id)
            self.assertEqual(finished.parent_span_id, llm_started.span_id)
            self.assertEqual(started.depth, 1)
            self.assertEqual(finished.depth, 1)
            self.assertTrue(finished.ok)
            self.assertEqual(finished.data["summary"], "dynamic:bridge-evidence")
            self.assertIsNotNone(started.started_at)
            self.assertIsNotNone(finished.finished_at)
            self.assertLessEqual(started.started_at, finished.finished_at)
            self.assertEqual(nested_context.trace_id, started.trace_id)
            self.assertEqual(nested_llm_started.trace_id, started.trace_id)
            self.assertEqual(nested_context.parent_span_id, started.span_id)
            self.assertEqual(nested_llm_started.parent_span_id, started.span_id)
            self.assertEqual(nested_context.depth, started.depth + 1)
            self.assertEqual(nested_llm_started.depth, started.depth + 1)
            self.assertLess(events.index(started), events.index(nested_context))
            self.assertLess(events.index(nested_context), events.index(nested_llm_started))
            self.assertLess(events.index(nested_llm_started), events.index(finished))
            self.assertLess(events.index(finished), events.index(llm_finished))
            self.assertEqual(len(handler_threads), 1)
            self.assertNotIn(handler_threads[0], set(event_threads))
            self.assertEqual(result.final_text, "done")
            backend.close_session(ref)

    def test_codex_event_sink_failure_does_not_turn_success_into_backend_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
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
                    session_id="failing-event-sink",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            completed = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"},
                    }
                ),
                "",
            )

            def failing_sink(_event: object) -> None:
                raise RuntimeError("telemetry consumer unavailable")

            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    return_value=completed,
                ),
                mock.patch("chatcopilot.agent.turn_support.LOGGER.exception"),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("continue despite telemetry failure"),
                    on_event=failing_sink,
                )

            self.assertEqual(result.stop_reason, "end_turn")
            self.assertEqual(result.final_text, "done")
            backend.close_session(ref)

    def test_codex_replaces_poisoned_relay_generation_after_timeout(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            tool_started = threading.Event()
            release_tool = threading.Event()

            def blocked_handler(_args):
                trace = current_trace()
                self.assertIsNotNone(trace)
                assert trace is not None
                self.assertIsNotNone(trace.sink)
                assert trace.sink is not None
                trace.sink(
                    ContextSnapshotPrepared(
                        snapshot_id="ctx_retired_generation_early",
                        backend="nested-timeout-test",
                        model="nested-model",
                        iteration=0,
                        session_messages=(),
                        effective_messages=(),
                        trace_id=trace.trace_id,
                        span_id="span_retired_generation",
                        parent_span_id=trace.span_id,
                        depth=trace.depth + 1,
                    )
                )
                tool_started.set()
                release_tool.wait(timeout=5)
                trace.sink(
                    LlmCallStarted(
                        model="late-nested-model",
                        iteration=1,
                        backend="nested-timeout-test",
                        trace_id=trace.trace_id,
                        span_id="span_retired_generation_late",
                        parent_span_id=trace.span_id,
                        depth=trace.depth + 1,
                    )
                )
                return "late result", [], None

            tool = ToolDef(
                name="blocked_tool",
                summary="Block until the test releases the retired relay generation.",
                properties={},
                required=[],
                handler=blocked_handler,
            )
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=1,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            backend = CodexAgentBackend(
                tool_names={"blocked_tool"},
                runtime_config=SimpleNamespace(routing=routing),
                tools=(tool,),
            )
            ref = backend.open_session(
                BackendOpenRequest(
                    session_id="relay-timeout-recovery",
                    prompt_plan=prompt_plan("system"),
                    allowed_tool_names=frozenset({"blocked_tool"}),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            invocation = 0
            relay_threads: list[threading.Thread] = []

            def run_turn(*_args, **_kwargs):
                nonlocal invocation
                invocation += 1
                if invocation == 1:
                    gateway = json.loads(
                        backend.native_session(ref).gateway_config.read_text(
                            encoding="utf-8"
                        )
                    )

                    def call_blocked_tool() -> None:
                        try:
                            call_session_relay(
                                gateway["relay"],
                                {
                                    "action": "call_tool",
                                    "name": "blocked_tool",
                                    "arguments": {},
                                },
                                timeout_seconds=5,
                            )
                        except Exception:
                            pass

                    relay_thread = threading.Thread(
                        target=call_blocked_tool,
                        daemon=True,
                    )
                    relay_threads.append(relay_thread)
                    relay_thread.start()
                    self.assertTrue(tool_started.wait(timeout=2))
                    raise subprocess.TimeoutExpired(["codex"], 1)
                return subprocess.CompletedProcess(
                    ["codex"],
                    0,
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "recovered"},
                        }
                    ),
                    "",
                )

            first_events: list[object] = []
            second_events: list[object] = []
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                        clear=False,
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
                        side_effect=run_turn,
                    ),
                ):
                    first = backend.stream_turn(
                        ref,
                        AgentTask("timeout"),
                        on_event=first_events.append,
                    )
                    release_tool.set()
                    for thread in relay_threads:
                        thread.join(timeout=2)
                    second = backend.stream_turn(
                        ref,
                        AgentTask("retry"),
                        on_event=second_events.append,
                    )
            finally:
                release_tool.set()

            self.assertEqual(first.stop_reason, "llm_error")
            self.assertEqual(second.stop_reason, "end_turn")
            self.assertEqual(second.final_text, "recovered")
            first_tool_started = next(
                event for event in first_events if isinstance(event, ToolStarted)
            )
            first_tool_finished = next(
                event for event in first_events if isinstance(event, ToolFinished)
            )
            self.assertEqual(first_tool_finished.span_id, first_tool_started.span_id)
            self.assertFalse(first_tool_finished.ok)
            self.assertEqual(
                first_tool_finished.error,
                "outcome_unknown_late_completion",
            )
            self.assertEqual(
                first_tool_finished.data,
                {
                    "outcome": "unknown",
                    "late_completion_possible": True,
                },
            )
            early_nested = next(
                event
                for event in first_events
                if isinstance(event, ContextSnapshotPrepared)
                and event.snapshot_id == "ctx_retired_generation_early"
            )
            self.assertEqual(early_nested.trace_id, first_tool_started.trace_id)
            self.assertEqual(early_nested.parent_span_id, first_tool_started.span_id)
            self.assertLess(first_events.index(first_tool_started), first_events.index(early_nested))
            self.assertFalse(
                any(
                    isinstance(event, LlmCallStarted)
                    and event.backend == "nested-timeout-test"
                    for event in first_events
                )
            )
            self.assertFalse(
                any(isinstance(event, (ToolStarted, ToolFinished)) for event in second_events)
            )
            self.assertFalse(
                any(
                    getattr(event, "backend", "") == "nested-timeout-test"
                    for event in second_events
                )
            )
            backend.close_session(ref)

    def test_codex_jsonl_projects_context_usage_and_safe_item_lifecycles(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            backend = CodexAgentBackend(
                tool_names={"dynamic_echo"},
                runtime_config=SimpleNamespace(routing=routing),
                tools=(_dynamic_tool(),),
            )
            ref = backend.open_session(
                BackendOpenRequest(
                    session_id="observable-codex",
                    prompt_plan=prompt_plan("system baseline"),
                    allowed_tool_names=frozenset({"dynamic_echo"}),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            stdout = "\n".join(
                json.dumps(event)
                for event in (
                    {"type": "thread.started", "thread_id": "observable-thread"},
                    {"type": "turn.started"},
                    {
                        "type": "item.started",
                        "item": {
                            "id": "cmd-1",
                            "type": "command_execution",
                            "command": "python -V",
                            "status": "in_progress",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "cmd-1",
                            "type": "command_execution",
                            "command": "python -V",
                            "aggregated_output": "private-command-output",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "text": "provider-private-reasoning",
                            "status": "completed",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "mcp-1",
                            "type": "mcp_tool_call",
                            "server": "chatcopilot",
                            "tool": "dynamic_echo",
                            "arguments": {"value": "private-argument"},
                            "result": "private-result",
                            "status": "completed",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "search-1",
                            "type": "web_search",
                            "query": "private-query",
                            "status": "completed",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "file-1",
                            "type": "file_change",
                            "changes": [{"path": "/private/path"}],
                            "status": "completed",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "plan-1",
                            "type": "plan_update",
                            "plan": [{"step": "private plan step"}],
                            "status": "completed",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {"id": "message-1", "type": "agent_message", "text": "done"},
                    },
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 40,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 12,
                            "cache_write_input_tokens": 8,
                        },
                    },
                )
            )
            completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
            events: list[object] = []
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    return_value=completed,
                ),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("inspect context", metadata={"trace_id": "trace-request-1"}),
                    on_event=events.append,
                )

            context = next(
                event for event in events if isinstance(event, ContextSnapshotPrepared)
            )
            started = next(event for event in events if isinstance(event, LlmCallStarted))
            finished = next(event for event in events if isinstance(event, LlmCallFinished))
            self.assertLess(events.index(context), events.index(started))
            self.assertEqual(context.backend, "codex")
            self.assertEqual(context.coverage, "adapter_visible")
            self.assertEqual(context.omitted, ("provider_internal_instructions",))
            self.assertEqual(context.trace_id, "trace-request-1")
            self.assertEqual(context.snapshot_id, started.context_snapshot_id)
            self.assertEqual(context.snapshot_id, finished.context_snapshot_id)
            self.assertEqual(context.session_messages[-1]["role"], "user")
            self.assertIn("inspect context", context.session_messages[-1]["content"])
            self.assertEqual(context.effective_messages[0]["role"], "user")
            self.assertIn("system baseline", context.effective_messages[0]["content"])
            self.assertIn("inspect context", context.effective_messages[0]["content"])
            self.assertEqual(context.tool_schemas[0]["name"], "dynamic_echo")
            self.assertGreater(context.estimated_tokens, 0)
            self.assertEqual(
                finished.usage,
                {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "reasoning_tokens": 12,
                    "cached_tokens": 40,
                    "cache_read_tokens": 40,
                    "cache_write_tokens": 8,
                },
            )
            span_starts = [event for event in events if isinstance(event, SpanStarted)]
            span_finishes = [event for event in events if isinstance(event, SpanFinished)]
            self.assertEqual(
                {event.kind for event in span_starts},
                {"command", "reasoning", "mcp_tool", "web_search", "file_change", "plan"},
            )
            self.assertEqual(len(span_starts), len(span_finishes))
            self.assertTrue(all(event.parent_span_id == started.span_id for event in span_starts))
            portable_events = repr(span_starts + span_finishes)
            for private_value in (
                "private-command-output",
                "provider-private-reasoning",
                "private-argument",
                "private-result",
                "private-query",
                "/private/path",
                "private plan step",
            ):
                self.assertNotIn(private_value, portable_events)
            self.assertEqual(result.final_text, "done")
            self.assertEqual(backend.current_session_ref(ref).value, "observable-thread")
            backend.close_session(ref)

    def test_resumed_codex_context_discloses_provider_managed_omission(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
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
                    session_id="resume-context",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            outputs = (
                subprocess.CompletedProcess(
                    ["codex"],
                    0,
                    "\n".join(
                        (
                            json.dumps(
                                {"type": "thread.started", "thread_id": "resume-native"}
                            ),
                            json.dumps(
                                {
                                    "type": "item.completed",
                                    "item": {"type": "agent_message", "text": "first"},
                                }
                            ),
                        )
                    ),
                    "",
                ),
                subprocess.CompletedProcess(
                    ["codex"],
                    0,
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "second"},
                        }
                    ),
                    "",
                ),
            )
            second_events: list[object] = []
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    side_effect=outputs,
                ),
            ):
                backend.stream_turn(ref, AgentTask("first"), on_event=lambda _: None)
                backend.stream_turn(
                    backend.current_session_ref(ref),
                    AgentTask("second"),
                    on_event=second_events.append,
                )

            context = next(
                event
                for event in second_events
                if isinstance(event, ContextSnapshotPrepared)
            )
            self.assertEqual(context.context_kind, "codex_native_resume")
            self.assertEqual(
                context.omitted,
                ("provider_internal_instructions", "provider_managed_resume_context"),
            )
            self.assertEqual(
                [message["role"] for message in context.session_messages],
                ["user", "assistant", "user"],
            )
            backend.close_session(backend.current_session_ref(ref))

    def test_codex_observability_streams_while_user_visible_output_stays_lease_gated(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
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
                    session_id="live-observability",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            events: list[object] = []

            def run_live(*_args, **kwargs):
                callback = kwargs["on_stdout_line"]
                self.assertTrue(
                    any(isinstance(event, ContextSnapshotPrepared) for event in events)
                )
                self.assertTrue(any(isinstance(event, LlmCallStarted) for event in events))
                self.assertFalse(any(isinstance(event, FinalText) for event in events))
                callback(
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "live-command",
                                "type": "command_execution",
                                "status": "in_progress",
                            },
                        }
                    )
                )
                self.assertTrue(any(isinstance(event, SpanStarted) for event in events))
                callback(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "live-command",
                                "type": "command_execution",
                                "exit_code": 0,
                                "status": "completed",
                            },
                        }
                    )
                )
                for _ in range(3):
                    callback("[stream line omitted: size limit exceeded]")
                callback(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "live done"},
                        }
                    )
                )
                callback(json.dumps({"type": "turn.completed"}))
                self.assertFalse(any(isinstance(event, LlmCallFinished) for event in events))
                self.assertFalse(any(isinstance(event, FinalText) for event in events))
                completed = subprocess.CompletedProcess(["codex"], 0, "", "")
                completed.stdout_line_truncated = True
                return completed

            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    side_effect=run_live,
                ),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("stream it"),
                    on_event=events.append,
                )

            self.assertEqual(result.final_text, "live done")
            self.assertEqual(result.stop_reason, "end_turn")
            self.assertEqual(
                [event.text for event in events if isinstance(event, FinalText)],
                ["live done"],
            )
            self.assertTrue(any(isinstance(event, LlmCallFinished) for event in events))
            omitted = next(
                event
                for event in events
                if isinstance(event, SpanFinished)
                and event.kind == "provider_omission"
                and event.data.get("reason") == "stream_record_size_limit"
            )
            self.assertFalse(omitted.ok)
            self.assertEqual(omitted.data.get("omitted_count"), 3)
            self.assertIn("Oversized provider JSONL", omitted.summary)
            backend.close_session(ref)

    def test_codex_oversized_jsonl_record_fails_explicitly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
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
                    session_id="oversized-jsonl",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            completed = subprocess.CompletedProcess(["codex"], 0, "", "")
            completed.stdout_line_truncated = True
            events: list[object] = []

            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    return_value=completed,
                ),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("oversized output"),
                    on_event=events.append,
                )

            error = next(event for event in events if isinstance(event, TurnError))
            self.assertEqual(result.stop_reason, "llm_error")
            self.assertIn("streaming size limit", error.message)
            self.assertNotEqual(
                result.final_text,
                "Codex completed without a final message.",
            )
            backend.close_session(ref)

    def test_codex_projector_marks_failed_turn_and_tolerates_structured_item_error(
        self,
    ) -> None:
        events: list[object] = []
        projector = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-test",
            llm_span_id="span-llm",
            parent_span_id="span-root",
            context_snapshot_id="ctx-test",
            on_event=events.append,
            on_thread_started=lambda _native_id: None,
        )

        projector.consume_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "malformed-error-shape",
                        "type": "command_execution",
                        "error": {"message": "provider detail"},
                    },
                }
            )
        )
        projector.consume_line(json.dumps({"type": "turn.failed"}))
        projector.finish(returncode=0)

        item_finish = next(event for event in events if isinstance(event, SpanFinished))
        llm_finish = next(event for event in events if isinstance(event, LlmCallFinished))
        self.assertFalse(item_finish.ok)
        self.assertFalse(llm_finish.ok)
        self.assertEqual(llm_finish.finish_reason, "failed")
        self.assertNotIn("provider detail", repr(events))

        incomplete_events: list[object] = []
        incomplete = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-incomplete",
            llm_span_id="span-llm-incomplete",
            parent_span_id="span-root",
            context_snapshot_id="ctx-incomplete",
            on_event=incomplete_events.append,
            on_thread_started=lambda _native_id: None,
        )
        incomplete.consume_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"id": "open-command", "type": "command_execution"},
                }
            )
        )
        incomplete.consume_line(json.dumps({"type": "turn.completed"}))
        incomplete.finish(returncode=0)
        incomplete_span = next(
            event for event in incomplete_events if isinstance(event, SpanFinished)
        )
        incomplete_llm = next(
            event for event in incomplete_events if isinstance(event, LlmCallFinished)
        )
        self.assertFalse(incomplete_span.ok)
        self.assertEqual(incomplete_span.data["status"], "incomplete")
        self.assertTrue(incomplete_llm.ok)

        conflicting_events: list[object] = []
        conflicting = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-conflict",
            llm_span_id="span-llm-conflict",
            parent_span_id="span-root",
            context_snapshot_id="ctx-conflict",
            on_event=conflicting_events.append,
            on_thread_started=lambda _native_id: None,
        )
        conflicting.consume_line(json.dumps({"type": "turn.completed"}))
        conflicting.finish(returncode=9)
        conflicting_llm = next(
            event for event in conflicting_events if isinstance(event, LlmCallFinished)
        )
        self.assertFalse(conflicting_llm.ok)
        self.assertEqual(conflicting_llm.finish_reason, "failed")

        nonfinite_events: list[object] = []
        nonfinite = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-nonfinite",
            llm_span_id="span-llm-nonfinite",
            parent_span_id="span-root",
            context_snapshot_id="ctx-nonfinite",
            on_event=nonfinite_events.append,
            on_thread_started=lambda _native_id: None,
        )
        nonfinite.consume_line(
            '{"type":"turn.completed","usage":'
            '{"input_tokens":1e999,"output_tokens":5,"cached_input_tokens":NaN}}'
        )
        nonfinite.finish(returncode=0)
        nonfinite_llm = next(
            event for event in nonfinite_events if isinstance(event, LlmCallFinished)
        )
        self.assertTrue(nonfinite_llm.ok)
        self.assertEqual(nonfinite_llm.usage["prompt_tokens"], 0)
        self.assertEqual(nonfinite_llm.usage["completion_tokens"], 5)
        self.assertEqual(nonfinite_llm.usage["cached_tokens"], 0)

    def test_codex_projector_ignores_pathological_json_and_bounds_usage(self) -> None:
        events: list[object] = []
        projector = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-pathological-json",
            llm_span_id="span-llm-pathological-json",
            parent_span_id="span-root",
            context_snapshot_id="ctx-pathological-json",
            on_event=events.append,
            on_thread_started=lambda _native_id: None,
        )

        projector.consume_line(
            '{"type":"turn.completed","usage":{"input_tokens":'
            + ("9" * 5000)
            + "}}"
        )
        projector.consume_line(("[" * 2000) + "0" + ("]" * 2000))
        projector.consume_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "intact reply"},
                }
            )
        )
        projector.consume_line(
            '{"type":"turn.completed","usage":{"input_tokens":'
            + ("9" * 1000)
            + ',"output_tokens":7}}'
        )
        projector.finish(returncode=0)

        llm_finish = next(event for event in events if isinstance(event, LlmCallFinished))
        self.assertEqual(projector.final_text, "intact reply")
        self.assertTrue(llm_finish.ok)
        self.assertEqual(llm_finish.usage["prompt_tokens"], 0)
        self.assertEqual(llm_finish.usage["completion_tokens"], 7)
        self.assertEqual(llm_finish.usage["total_tokens"], 7)

    def test_codex_projector_saturates_derived_usage_total(self) -> None:
        max_count = (1 << 63) - 1

        def projected_usage(usage: dict[str, int]) -> dict[str, int]:
            events: list[object] = []
            projector = CodexJsonlProjector(
                model="gpt-test",
                iteration=0,
                trace_id="trace-usage-bound",
                llm_span_id="span-usage-bound",
                parent_span_id="span-root",
                context_snapshot_id="ctx-usage-bound",
                on_event=events.append,
                on_thread_started=lambda _native_id: None,
            )
            projector.consume_line(
                json.dumps({"type": "turn.completed", "usage": usage})
            )
            projector.finish(returncode=0)
            finished = next(
                event for event in events if isinstance(event, LlmCallFinished)
            )
            return dict(finished.usage or {})

        saturated = projected_usage(
            {"input_tokens": max_count, "output_tokens": max_count}
        )
        self.assertEqual(saturated["prompt_tokens"], max_count)
        self.assertEqual(saturated["completion_tokens"], max_count)
        self.assertEqual(saturated["total_tokens"], max_count)

        fallback = projected_usage(
            {"input_tokens": 3, "output_tokens": 4, "total_tokens": max_count + 1}
        )
        self.assertEqual(fallback["total_tokens"], 7)

    def test_codex_projector_bounds_provider_items_and_final_text(self) -> None:
        events: list[object] = []
        projector = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-bounded-projector",
            llm_span_id="span-llm-bounded-projector",
            parent_span_id="span-root",
            context_snapshot_id="ctx-bounded-projector",
            on_event=events.append,
            on_thread_started=lambda _native_id: None,
        )

        for index in range(600):
            projector.consume_line(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"command-{index}",
                            "type": "command_execution",
                            "status": "completed",
                        },
                    }
                )
            )
        for text in ("a" * (600 * 1024), "b" * (600 * 1024)):
            projector.consume_line(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": text},
                    }
                )
            )
        projector.finish(returncode=0)

        omission_finishes = [
            event
            for event in events
            if isinstance(event, SpanFinished)
            and event.kind == "provider_omission"
            and event.data.get("status") == "truncated"
        ]
        self.assertEqual(len(projector._completed_items), 500)
        self.assertEqual(projector.provider_item_omission_count, 100)
        self.assertEqual(len(omission_finishes), 1)
        self.assertEqual(omission_finishes[0].data.get("omitted_count"), 100)
        self.assertTrue(projector.final_text_truncated)
        self.assertLessEqual(len(projector.final_text), 1024 * 1024)

        stale_final = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-stale-final",
            llm_span_id="span-stale-final",
            parent_span_id="span-root",
            context_snapshot_id="ctx-stale-final",
            on_event=lambda _event: None,
            on_thread_started=lambda _native_id: None,
        )
        stale_final.consume_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "stale reply"},
                }
            )
        )
        stale_final.consume_line("[stream line omitted: size limit exceeded]")
        self.assertFalse(stale_final.has_complete_final_after_stream_omission)
        stale_final.consume_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "fresh reply"},
                }
            )
        )
        self.assertTrue(stale_final.has_complete_final_after_stream_omission)

        metadata_events: list[object] = []
        metadata = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-bounded-metadata",
            llm_span_id="span-bounded-metadata",
            parent_span_id="span-root",
            context_snapshot_id="ctx-bounded-metadata",
            on_event=metadata_events.append,
            on_thread_started=lambda _native_id: None,
        )
        metadata.consume_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "i" * (100 * 1024),
                        "type": "mcp_tool_call",
                        "server": "s" * (100 * 1024),
                        "tool": "t" * (100 * 1024),
                    },
                }
            )
        )
        metadata_start = next(
            event for event in metadata_events if isinstance(event, SpanStarted)
        )
        self.assertLessEqual(len(next(iter(metadata._active_spans))), 33)
        self.assertLessEqual(len(metadata_start.name), 240)

    def test_codex_projector_counts_omitted_started_completed_pair_once(self) -> None:
        events: list[object] = []
        projector = CodexJsonlProjector(
            model="gpt-test",
            iteration=0,
            trace_id="trace-paired-provider-cap",
            llm_span_id="span-paired-provider-cap",
            parent_span_id="span-root",
            context_snapshot_id="ctx-paired-provider-cap",
            on_event=events.append,
            on_thread_started=lambda _native_id: None,
        )
        for index in range(500):
            projector.consume_line(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"retained-{index}",
                            "type": "command_execution",
                            "status": "completed",
                        },
                    }
                )
            )
        omitted_item = {
            "id": "paired-over-limit",
            "type": "command_execution",
            "status": "completed",
        }
        projector.consume_line(
            json.dumps({"type": "item.started", "item": omitted_item})
        )
        projector.consume_line(
            json.dumps({"type": "item.completed", "item": omitted_item})
        )
        projector.finish(returncode=0)

        omission = next(
            event
            for event in events
            if isinstance(event, SpanFinished)
            and event.kind == "provider_omission"
        )
        self.assertEqual(projector.provider_item_omission_count, 1)
        self.assertEqual(omission.data.get("omitted_count"), 1)

    def test_task_metadata_selects_model_without_mutating_runtime_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-5.6-terra",
                code_reasoning_effort="medium",
                code_profiles={
                    "sol-max": CodeModelProfile(
                        model="gpt-5.6-sol",
                        reasoning_effort="max",
                    )
                },
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            backend = CodexAgentBackend(
                tool_names=set(),
                runtime_config=SimpleNamespace(routing=routing),
            )
            ref = backend.open_session(
                BackendOpenRequest(
                    session_id="model-selection",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            selection = CodeModelSelection(
                provider="codex_cli",
                model="gpt-5.6-sol",
                reasoning_effort="max",
                scope="once",
                source="profile",
                profile="sol-max",
            )
            completed = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"},
                    }
                ),
                "",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask(
                        "use selected model",
                        metadata={CODE_MODEL_SELECTION_METADATA_KEY: selection.to_payload()},
                    ),
                    on_event=lambda _: None,
                )

            command = run.call_args.args[0]
            self.assertEqual(result.final_text, "done")
            self.assertEqual(
                command[:4],
                ["/usr/bin/codex", "exec", "--model", "gpt-5.6-sol"],
            )
            self.assertIn('model_reasoning_effort="max"', command)
            self.assertEqual(routing.code_model, "gpt-5.6-terra")
            self.assertEqual(routing.code_reasoning_effort, "medium")
            backend.close_session(ref)

    def test_resume_id_survives_backend_object_reconstruction(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            kwargs = {
                "tool_names": {"dynamic_echo"},
                "runtime_config": SimpleNamespace(routing=routing),
                "tools": (_dynamic_tool(),),
            }
            request = BackendOpenRequest(
                session_id="acp-persisted",
                prompt_plan=prompt_plan("system", backend="codex"),
                allowed_tool_names=frozenset({"dynamic_echo"}),
                options={
                    "workspace_root": root,
                    "backend_state_root": root / "state",
                },
            )
            backend = CodexAgentBackend(**kwargs)
            ref = backend.open_session(request)
            completed = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps({"type": "thread.started", "thread_id": "persisted-native-id"}),
                "",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    return_value=completed,
                ),
            ):
                backend.stream_turn(ref, AgentTask("one"), on_event=lambda _: None)
            native_ref = backend.current_session_ref(ref)
            backend.close_session(native_ref)

            reconstructed = CodexAgentBackend(**kwargs)
            restored_ref = reconstructed.open_session(request)
            self.assertEqual(restored_ref.value, "persisted-native-id")
            with mock.patch(
                "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                return_value="/usr/bin/codex",
            ):
                command = reconstructed._command(reconstructed.native_session(restored_ref))
            resume_index = command.index("resume")
            self.assertEqual(
                command[resume_index:],
                ["resume", "persisted-native-id", "-"],
            )
            self.assertLess(command.index("--sandbox"), resume_index)
            self.assertLess(command.index("--cd"), resume_index)
            self.assertLess(command.index("--json"), resume_index)
            reconstructed.close_session(restored_ref)

    def test_disabled_persisted_resume_starts_fresh_after_reconstruction(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            kwargs = {
                "tool_names": {"dynamic_echo"},
                "runtime_config": SimpleNamespace(routing=routing),
                "tools": (_dynamic_tool(),),
            }
            request = BackendOpenRequest(
                session_id="acp-no-persisted-resume",
                prompt_plan=prompt_plan("system", backend="codex"),
                allowed_tool_names=frozenset({"dynamic_echo"}),
                options={
                    "workspace_root": root,
                    "backend_state_root": root / "state",
                    "restore_persisted_native_session": False,
                },
            )
            backend = CodexAgentBackend(**kwargs)
            ref = backend.open_session(request)
            completed = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps(
                    {"type": "thread.started", "thread_id": "stale-group-thread"}
                ),
                "",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
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
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    return_value=completed,
                ),
            ):
                backend.stream_turn(ref, AgentTask("one"), on_event=lambda _: None)
            backend.close_session(backend.current_session_ref(ref))

            reconstructed = CodexAgentBackend(**kwargs)
            fresh_ref = reconstructed.open_session(request)
            fresh_state = reconstructed.native_session(fresh_ref)
            self.assertEqual(fresh_state.native_session_id, "")
            with mock.patch(
                "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                return_value="/usr/bin/codex",
            ):
                command = reconstructed._command(fresh_state)
            self.assertNotIn("resume", command)
            reconstructed.close_session(fresh_ref)

    def test_role_change_discards_persisted_native_resume_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            kwargs = {
                "tool_names": set(),
                "runtime_config": SimpleNamespace(routing=routing),
            }
            user_request = BackendOpenRequest(
                session_id="role-change",
                prompt_plan=prompt_plan("system"),
                options={
                    "workspace_root": root,
                    "backend_state_root": root / "state",
                    "role_hint": "user",
                },
            )
            backend = CodexAgentBackend(**kwargs)
            ref = backend.open_session(user_request)
            state = backend.native_session(ref)
            state.native_session_id = "old-elevated-thread"
            backend._persist_session_state(state)
            backend.close_session(ref)

            owner_request = BackendOpenRequest(
                session_id="role-change",
                prompt_plan=prompt_plan("system"),
                options={
                    "workspace_root": root,
                    "backend_state_root": root / "state",
                    "role_hint": "admin",
                },
            )
            reconstructed = CodexAgentBackend(**kwargs)
            restored_ref = reconstructed.open_session(owner_request)

            self.assertNotEqual(restored_ref.value, "old-elevated-thread")
            self.assertEqual(
                reconstructed.native_session(restored_ref).native_session_id,
                "",
            )
            reconstructed.close_session(restored_ref)

    def test_caller_identity_change_discards_persisted_native_resume_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            routing = SimpleNamespace(
                code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test",
                code_reasoning_effort="medium",
                code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
            )
            kwargs = {
                "tool_names": set(),
                "runtime_config": SimpleNamespace(routing=routing),
            }
            request = BackendOpenRequest(
                session_id="identity-change",
                prompt_plan=prompt_plan("system"),
                caller_identity=SessionIdentity(user_id="caller-a"),
                options={
                    "workspace_root": root,
                    "backend_state_root": root / "state",
                },
            )
            backend = CodexAgentBackend(**kwargs)
            ref = backend.open_session(request)
            state = backend.native_session(ref)
            state.native_session_id = "old-caller-thread"
            backend._persist_session_state(state)
            backend.close_session(ref)

            reconstructed = CodexAgentBackend(**kwargs)
            restored_ref = reconstructed.open_session(
                BackendOpenRequest(
                    session_id="identity-change",
                    prompt_plan=prompt_plan("system"),
                    caller_identity=SessionIdentity(user_id="caller-b"),
                    options={
                        "workspace_root": root,
                        "backend_state_root": root / "state",
                    },
                )
            )

            self.assertNotEqual(restored_ref.value, "old-caller-thread")
            self.assertEqual(
                reconstructed.native_session(restored_ref).native_session_id,
                "",
            )
            reconstructed.close_session(restored_ref)

    def test_explicit_login_generation_discards_old_native_resume_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root, token="first-account")
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
                    session_id="generation-change",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            first = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps({"type": "thread.started", "thread_id": "old-account-thread"}),
                "",
            )
            second = subprocess.CompletedProcess(
                ["codex"],
                0,
                json.dumps({"type": "thread.started", "thread_id": "new-account-thread"}),
                "",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
                ),
                mock.patch(
                    "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                    return_value="/usr/bin/codex",
                ),
                mock.patch(
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    side_effect=[first, second],
                ) as run,
            ):
                backend.stream_turn(ref, AgentTask("first"), on_event=lambda _: None)
                old_ref = backend.current_session_ref(ref)
                self.assertEqual(old_ref.value, "old-account-thread")

                _main_auth_root(root, token="second-account")
                backend.stream_turn(
                    old_ref,
                    AgentTask("second"),
                    on_event=lambda _: None,
                )

            self.assertNotIn("old-account-thread", run.call_args_list[1].args[0])
            self.assertEqual(
                backend.current_session_ref(old_ref).value,
                "new-account-thread",
            )
            state = backend.native_session(BackendSessionRef("codex", "new-account-thread"))
            self.assertEqual(state.credential_generation, 2)
            self.assertEqual(state.native_session_id, "new-account-thread")
            backend.close_session(BackendSessionRef("codex", "new-account-thread"))

    def test_auth_stderr_is_diagnostic_only_and_refresh_is_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
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
                    session_id="auth-error",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            raw_error = (
                "401 Unauthorized: refresh token already used; secret-token-must-stay-private"
            )

            def failed_with_refresh(*_args, **kwargs):
                runtime_auth = Path(kwargs["env"]["CODEX_HOME"]) / "auth.json"
                runtime_auth.write_text(
                    json.dumps(_codex_auth_payload("rotated-on-failure")),
                    encoding="utf-8",
                )
                runtime_auth.chmod(0o600)
                return subprocess.CompletedProcess(["codex"], 1, "", raw_error)

            events: list[object] = []
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
                ),
                mock.patch(
                    "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                    return_value="/usr/bin/codex",
                ),
                mock.patch(
                    "chatcopilot.agent.backends.codex.run_codex_process",
                    side_effect=failed_with_refresh,
                ),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("hello"),
                    on_event=events.append,
                )

            self.assertEqual(result.stop_reason, "llm_error")
            self.assertIn("codex-auth login", result.final_text)
            self.assertNotIn("secret-token", result.final_text)
            errors = [event for event in events if isinstance(event, TurnError)]
            finals = [event for event in events if isinstance(event, FinalText)]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].code, "codex_auth_invalid")
            self.assertIn("secret-token-must-stay-private", errors[0].message)
            self.assertEqual([event.text for event in finals], [result.final_text])
            authority = json.loads((auth_root / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(
                authority["tokens"]["refresh_token"],
                "rotated-on-failure",
            )
            backend.close_session(ref)

    def test_success_without_agent_message_never_promotes_stderr(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
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
                    session_id="empty-success",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "backend_state_root": root / "state",
                    },
                )
            )
            completed = subprocess.CompletedProcess(
                ["codex"],
                0,
                "",
                "private warning should not be a reply",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)},
                    clear=False,
                ),
                mock.patch(
                    "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                    return_value="/usr/bin/codex",
                ),
                mock.patch(
                    "chatcopilot.external_tools.codex_cli.process_runner.subprocess.run",
                    return_value=completed,
                ),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("hello"),
                    on_event=lambda _: None,
                )

            self.assertEqual(
                result.final_text,
                "Codex completed without a final message.",
            )
            self.assertNotIn("private warning", result.final_text)
            backend.close_session(ref)


class CodexBackendPolicyTests(TestCase):
    @staticmethod
    def _routing() -> SimpleNamespace:
        return SimpleNamespace(
            code_command="codex exec --model {model} --cd {workdir}",
            code_model="gpt-test",
            code_reasoning_effort="medium",
            code_timeout_seconds=30,
            code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR",
        )

    def _command_and_prompt(
        self,
        root: Path,
        policy: CodexMainSessionPolicy | None = None,
        *,
        role_hint: str = "user",
        caller_user_id: str | None = "123",
    ) -> tuple[list[str], str]:
        backend = CodexAgentBackend(
            tool_names={"dynamic_echo"},
            runtime_config=SimpleNamespace(routing=self._routing()),
            tools=(_dynamic_tool(),),
            backend_policy=policy,
        )
        ref = backend.open_session(
            BackendOpenRequest(
                session_id="policy-session",
                prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({"dynamic_echo"}),
                caller_identity=(
                    SessionIdentity(user_id=caller_user_id) if caller_user_id is not None else None
                ),
                options={
                    "workspace_root": root,
                    "source_root": root,
                    "backend_state_root": root / "state",
                    "role_hint": role_hint,
                },
            )
        )
        state = backend.native_session(ref)
        with mock.patch(
            "chatcopilot.external_tools.codex_cli.command._resolve_executable",
            return_value="/usr/bin/codex",
        ):
            command = backend._command(state)
        prompt = backend._prompt(state, AgentTask("do work"))
        backend.close_session(ref)
        return command, prompt

    def _policy_fingerprint(self, policy: CodexMainSessionPolicy) -> str:
        backend = CodexAgentBackend(
            tool_names=set(),
            runtime_config=SimpleNamespace(routing=self._routing()),
            backend_policy=policy,
        )
        return backend._policy_fingerprint(
            "user",
            "workspace",
            caller_user_id="123",
        )

    def test_default_policy_uses_isolated_member_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            command, prompt = self._command_and_prompt(root)

        self.assertIn("workspace-write", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[command.index("--cd") + 1], str(root))
        self.assertTrue(any(f'HOME = "{root}"' in item for item in command))
        self.assertIn("sandbox_workspace_write.network_access=true", command)
        self.assertIn('web_search="live"', command)
        self.assertIn("features.network_proxy.enabled=true", command)
        self.assertIn('features.network_proxy.domains={ "*" = "allow" }', command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertIn("personal workspace", prompt)

    def test_owner_worktree_policy_is_source_read_only_and_uses_gateway(self) -> None:
        policy = CodexMainSessionPolicy(
            owner_access="worktree",
            member_access="workspace",
        )
        with TemporaryDirectory() as tmp:
            command, prompt = self._command_and_prompt(Path(tmp), policy, role_hint="owner")

        self.assertIn("read-only", command)
        self.assertNotIn("--skip-git-repo-check", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("mcp_servers={}", command)
        self.assertTrue(any("mcp_servers.chatcopilot.command" in item for item in command))
        self.assertIn("mcp_servers.chatcopilot.required=true", command)
        self.assertIn(
            'mcp_servers.chatcopilot.default_tools_approval_mode="approve"',
            command,
        )
        enabled_tools = next(
            item
            for item in command
            if item.startswith("mcp_servers.chatcopilot.enabled_tools=")
        )
        self.assertIn('"dynamic_echo"', enabled_tools)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertIn("read-only", prompt)
        self.assertIn("start_code_task", prompt)
        self.assertIn("plan without calling start_code_task", prompt)
        self.assertIn("submit the complete approved plan exactly once", prompt)

    def test_eval_confinement_disables_command_network_and_web_search(self) -> None:
        policy = CodexMainSessionPolicy(
            network_access=False,
            web_search_mode="disabled",
            sandbox_mode="read-only",
        )
        with TemporaryDirectory() as tmp:
            command, prompt = self._command_and_prompt(Path(tmp), policy)

        self.assertIn("read-only", command)
        self.assertIn("sandbox_workspace_write.network_access=false", command)
        self.assertIn('web_search="disabled"', command)
        self.assertNotIn("features.network_proxy.enabled=true", command)
        self.assertNotIn('features.network_proxy.domains={ "*" = "allow" }', command)
        self.assertIn("web search is disabled", prompt)

    def test_command_confinement_changes_policy_fingerprint(self) -> None:
        baseline = self._policy_fingerprint(CodexMainSessionPolicy())

        variants = (
            CodexMainSessionPolicy(network_access=False),
            CodexMainSessionPolicy(web_search_mode="disabled"),
            CodexMainSessionPolicy(sandbox_mode="read-only"),
            CodexMainSessionPolicy(allow_delegate_tools=True),
            CodexMainSessionPolicy(allow_unified_search_tool=True),
        )

        for policy in variants:
            with self.subTest(policy=policy):
                self.assertNotEqual(self._policy_fingerprint(policy), baseline)

    def test_eval_tool_surface_policy_requires_strict_booleans(self) -> None:
        for field_name in (
            "allow_delegate_tools",
            "allow_unified_search_tool",
        ):
            with (
                self.subTest(field=field_name),
                self.assertRaisesRegex(
                    TypeError,
                    field_name,
                ),
            ):
                CodexMainSessionPolicy(**{field_name: 1})

    def test_worktree_policy_cannot_override_read_only_with_writable_sandbox(self) -> None:
        policy = CodexMainSessionPolicy(
            owner_access="worktree",
            sandbox_mode="workspace-write",
        )
        with (
            TemporaryDirectory() as tmp,
            self.assertRaisesRegex(
                ValueError,
                "cannot use a writable sandbox",
            ),
        ):
            self._command_and_prompt(Path(tmp), policy, role_hint="owner")


class SessionToolRelayTests(TestCase):
    def test_agent_runtime_passes_session_payload_filter_to_codex_backend(self) -> None:
        payload_filter = lambda payload: dict(payload)  # noqa: E731
        runtime = AgentRuntime(
            llm=mock.Mock(),
            tools=(),
            tools_schema=(),
            runtime_config=ChatConfig(),
            agent_backend="codex",
        )
        backend = mock.Mock()
        backend.capabilities = BackendCapabilities(
            names=frozenset({CAPABILITY_CHAT}),
            tool_names=frozenset(),
        )
        backend.open_session.return_value = BackendSessionRef("codex", "relay-filter")

        with mock.patch(
            "chatcopilot.agent.runtime.build_backend",
            return_value=backend,
        ) as build:
            session = runtime.new_session(
                session_id="relay-filter",
                prompt_input=prompt_input("system", backend="codex"),
                payload_filter=payload_filter,
            )

        self.assertIs(build.call_args.kwargs["tool_payload_filter"], payload_filter)
        session.close()

    def test_dynamic_tool_uses_live_executor_and_wrong_token_fails_closed(self) -> None:
        calls: list[str] = []
        tool = _dynamic_tool(calls)
        relay = SessionToolRelay(
            tools=(tool,),
            executor=ToolExecutor(tools=[tool]),
        )
        endpoint = relay.start()
        try:
            listed = call_session_relay(endpoint.to_dict(), {"action": "list_tools"})
            called = call_session_relay(
                endpoint.to_dict(),
                {
                    "action": "call_tool",
                    "name": "dynamic_echo",
                    "arguments": {"value": "same-executor"},
                },
            )
            invalid = endpoint.to_dict()
            invalid["token"] = "wrong"
            denied = call_session_relay(invalid, {"action": "list_tools"})
        finally:
            relay.close()

        self.assertEqual([tool["name"] for tool in listed["tools"]], ["dynamic_echo"])
        self.assertTrue(called["result"]["ok"])
        self.assertEqual(calls, ["same-executor"])
        events = relay.drain_tool_events()
        self.assertEqual([event["type"] for event in events], ["tool_started", "tool_finished"])
        self.assertEqual(events[0]["arguments"], {"value": "same-executor"})
        self.assertEqual(events[0]["call_id"], events[1]["call_id"])
        self.assertLessEqual(events[0]["started_at"], events[1]["finished_at"])
        self.assertTrue(events[1]["ok"])
        self.assertEqual(events[1]["data"]["summary"], "dynamic:same-executor")
        self.assertFalse(denied["ok"])
        self.assertIn("authentication", denied["error"])

    def test_filtered_handler_result_is_used_for_response_and_audit_event(self) -> None:
        private_path = str((Path.cwd() / "relay-sensitive" / "source.py").resolve())

        def handler(_args):
            raise RuntimeError(f"failed at {private_path}")

        tool = ToolDef(
            name="sensitive_tool",
            summary="Sensitive failure.",
            properties={},
            required=[],
            handler=handler,
        )
        seen: list[dict[str, object]] = []

        def sanitize(payload):
            seen.append(dict(payload))
            return {
                "ok": False,
                "error": "request failed",
                "error_code": "request_failed",
            }

        relay = SessionToolRelay(
            tools=(tool,),
            executor=ToolExecutor(tools=[tool]),
            payload_filter=sanitize,
        )
        endpoint = relay.start()
        try:
            response = call_session_relay(
                endpoint.to_dict(),
                {"action": "call_tool", "name": "sensitive_tool", "arguments": {}},
            )
        finally:
            relay.close()

        events = relay.drain_tool_events()
        serialized = json.dumps({"response": response, "events": events})
        self.assertIn(private_path, str(seen[0]["error"]))
        self.assertNotIn(private_path, serialized)
        self.assertEqual(
            response["result"],
            {
                "tool": "sensitive_tool",
                "ok": False,
                "error": "request failed",
                "error_code": "request_failed",
            },
        )
        self.assertEqual(events[1]["error"], "request failed")
        expected_event_data = dict(response["result"])
        expected_event_data.pop("tool")
        self.assertEqual(events[1]["data"], expected_event_data)

    def test_identity_filter_preserves_success_payload(self) -> None:
        private_path = str((Path.cwd() / "relay-owner" / "report.txt").resolve())

        class OwnerExecutor:
            def execute(self, _name, _arguments):
                return ToolResult(
                    ok=True,
                    summary=f"created {private_path}",
                    outputs=[private_path],
                    console=f"created {private_path}",
                    doc_links=[],
                )

        tool = ToolDef(
            name="owner_tool",
            summary="Owner-only result.",
            properties={},
            required=[],
            handler=lambda _args: ("", [], None),
        )
        relay = SessionToolRelay(
            tools=(tool,),
            executor=OwnerExecutor(),
            payload_filter=lambda payload: dict(payload),
        )
        endpoint = relay.start()
        try:
            response = call_session_relay(
                endpoint.to_dict(),
                {"action": "call_tool", "name": "owner_tool", "arguments": {}},
            )
        finally:
            relay.close()

        events = relay.drain_tool_events()
        self.assertEqual(response["result"]["outputs"], [private_path])
        self.assertIn(private_path, response["result"]["summary"])
        self.assertIn(private_path, response["result"]["console_tail"])
        self.assertEqual(events[1]["data"]["outputs"], [private_path])

    def test_executor_or_filter_exception_returns_only_generic_payload(self) -> None:
        secret = str((Path.cwd() / "relay-private" / "traceback.py").resolve())
        tool = _dynamic_tool()

        class ExplodingExecutor:
            def execute(self, _name, _arguments):
                raise RuntimeError(f"traceback at {secret}")

        def exploding_filter(_payload):
            raise RuntimeError(f"filter failed at {secret}")

        cases = (
            (ExplodingExecutor(), None),
            (ToolExecutor(tools=[tool]), exploding_filter),
        )
        for executor, payload_filter in cases:
            with self.subTest(payload_filter=payload_filter is not None):
                relay = SessionToolRelay(
                    tools=(tool,),
                    executor=executor,
                    payload_filter=payload_filter,
                )
                endpoint = relay.start()
                try:
                    response = call_session_relay(
                        endpoint.to_dict(),
                        {
                            "action": "call_tool",
                            "name": "dynamic_echo",
                            "arguments": {"value": "value"},
                        },
                    )
                finally:
                    relay.close()

                events = relay.drain_tool_events()
                serialized = json.dumps({"response": response, "events": events})
                self.assertNotIn(secret, serialized)
                self.assertNotIn("traceback", serialized.lower())
                self.assertEqual(
                    response["result"],
                    {
                        "tool": "dynamic_echo",
                        "ok": False,
                        "error": "tool execution failed",
                        "error_code": "tool_execution_failed",
                    },
                )
                self.assertEqual(events[1]["data"], {
                    "ok": False,
                    "error": "tool execution failed",
                    "error_code": "tool_execution_failed",
                })

    def test_nested_event_overflow_preserves_tool_result_and_projects_one_omission(
        self,
    ) -> None:
        nested_event_count = 1027
        overflow_recorded = threading.Event()
        release_tool = threading.Event()

        def handler(_args):
            trace = current_trace()
            self.assertIsNotNone(trace)
            assert trace is not None
            self.assertIsNotNone(trace.sink)
            assert trace.sink is not None
            for index in range(nested_event_count):
                trace.sink(
                    LlmCallStarted(
                        model="nested-overflow-model",
                        iteration=index,
                        backend="nested-overflow-test",
                        trace_id=trace.trace_id,
                        span_id=f"span_nested_overflow_{index}",
                        parent_span_id=trace.span_id,
                        depth=trace.depth + 1,
                    )
                )
            overflow_recorded.set()
            release_tool.wait(timeout=5)
            return "tool succeeded despite telemetry overflow", [], None

        tool = ToolDef(
            name="overflow_tool",
            summary="Emit more nested events than the bounded relay can retain.",
            properties={},
            required=[],
            handler=handler,
        )
        relay = SessionToolRelay(
            tools=(tool,),
            executor=ToolExecutor(tools=[tool]),
        )
        endpoint = relay.start()
        generation = relay.begin_turn(
            trace_id="trace_relay_overflow",
            parent_span_id="span_codex_llm",
            depth=1,
        )
        projected: list[object] = []
        sink_calls = 0
        called_holder: dict[str, object] = {}

        def intermittently_failing_sink(event: object) -> None:
            nonlocal sink_calls
            sink_calls += 1
            projected.append(event)
            if sink_calls == 2:
                raise RuntimeError("recorder unavailable once")

        def call_tool() -> None:
            called_holder["response"] = call_session_relay(
                endpoint.to_dict(),
                {
                    "action": "call_tool",
                    "name": "overflow_tool",
                    "arguments": {},
                },
            )

        caller = threading.Thread(target=call_tool, daemon=True)
        try:
            caller.start()
            self.assertTrue(overflow_recorded.wait(timeout=2))
            with mock.patch("chatcopilot.agent.turn_support.LOGGER.exception") as logged:
                live_audit_error = CodexAgentBackend._emit_relay_tool_events(
                    relay,
                    intermittently_failing_sink,
                    generation=generation,
                    trace_id="trace_relay_overflow",
                    parent_span_id="span_codex_llm",
                    require_complete=False,
                )
                release_tool.set()
                caller.join(timeout=2)
                audit_error = CodexAgentBackend._emit_relay_tool_events(
                    relay,
                    intermittently_failing_sink,
                    generation=generation,
                    trace_id="trace_relay_overflow",
                    parent_span_id="span_codex_llm",
                    require_complete=True,
                )
        finally:
            release_tool.set()
            caller.join(timeout=2)
            relay.end_turn(generation)
            relay.close()

        self.assertFalse(caller.is_alive())
        called = called_holder["response"]
        self.assertIsInstance(called, dict)
        assert isinstance(called, dict)
        self.assertTrue(called["result"]["ok"])
        self.assertEqual(
            called["result"]["summary"],
            "tool succeeded despite telemetry overflow",
        )
        self.assertEqual(live_audit_error, "")
        self.assertEqual(audit_error, "")
        logged.assert_called_once()
        nested = [
            event
            for event in projected
            if isinstance(event, LlmCallStarted)
            and event.backend == "nested-overflow-test"
        ]
        self.assertEqual(len(nested), 1022)
        finished = next(event for event in projected if isinstance(event, ToolFinished))
        self.assertTrue(finished.ok)
        omissions = [
            event
            for event in projected
            if isinstance(event, SpanFinished)
            and event.kind == "provider_omission"
            and event.data.get("reason") == "relay_nested_event_buffer_limit"
        ]
        self.assertEqual(len(omissions), 1)
        self.assertEqual(omissions[0].trace_id, "trace_relay_overflow")
        self.assertEqual(omissions[0].parent_span_id, finished.span_id)
        self.assertEqual(omissions[0].data.get("omitted_count"), 5)
        self.assertEqual(omissions[0].data.get("projected_event_limit"), 1024)
        self.assertLess(projected.index(omissions[0]), projected.index(finished))


class BackendStateTransitionTests(TestCase):
    def test_switch_deletes_old_state_before_target_start_and_never_restores(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            prepare_backend_deployment(
                instance_id="demo", target_backend="native", workspace_root=root
            )
            transcript = root / "p2p_user" / "transcripts"
            backend_state = root / "p2p_user" / ".backend-sessions"
            group_backend_state = (
                root
                / "group_demo"
                / ".conversation-state"
                / "backend-sessions"
                / "actor-digest"
            )
            group_journal = (
                root
                / "group_demo"
                / ".conversation-state"
                / "group-conversation.jsonl"
            )
            transcript.mkdir(parents=True)
            backend_state.mkdir(parents=True)
            group_backend_state.mkdir(parents=True)
            group_journal.write_text("shared history", encoding="utf-8")
            (transcript / "turn.jsonl").write_text("history", encoding="utf-8")

            transition = prepare_backend_deployment(
                instance_id="demo", target_backend="codex", workspace_root=root
            )
            try:
                raise RuntimeError("target deployment failed")
            except RuntimeError:
                pass

            self.assertTrue(transition.state_deleted)
            self.assertFalse(transcript.exists())
            self.assertFalse(backend_state.exists())
            self.assertFalse(group_backend_state.exists())
            self.assertTrue(group_journal.is_file())
            marker = json.loads((root / ".agent-backend.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["backend"], "codex")
            events = [
                json.loads(line)["event"]
                for line in transition.audit_path.read_text(encoding="utf-8").splitlines()
            ]
            switch_events = events[-2:]
            self.assertEqual(switch_events, ["state_deleted", "target_deploy_started"])

    def test_unchanged_backend_preserves_histories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            prepare_backend_deployment(
                instance_id="demo", target_backend="native", workspace_root=root
            )
            transcript = root / "user" / "transcripts"
            transcript.mkdir(parents=True)
            (transcript / "turn.jsonl").write_text("history", encoding="utf-8")
            transition = prepare_backend_deployment(
                instance_id="demo", target_backend="native", workspace_root=root
            )
            self.assertFalse(transition.state_deleted)
            self.assertTrue(transcript.exists())


class TypedTurnPipelineTests(IsolatedAsyncioTestCase):
    async def test_handlers_run_in_the_fixed_order(self) -> None:
        seen: list[str] = []

        def handler(name: str) -> CallbackTurnHandler:
            async def callback(_context: TurnContext) -> TurnOutcome:
                seen.append(name)
                return TurnOutcome()

            return CallbackTurnHandler(name, callback)

        pipeline = OrderedTurnPipeline(tuple(handler(name) for name in TURN_STAGE_ORDER))
        context = TurnContext("sid", object(), "hello", None)
        await pipeline.run(context)

        self.assertEqual(tuple(seen), TURN_STAGE_ORDER)
        self.assertEqual(tuple(context.completed_stages), TURN_STAGE_ORDER)
