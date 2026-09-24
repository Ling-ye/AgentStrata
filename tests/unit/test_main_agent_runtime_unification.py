from __future__ import annotations

from chatcopilot.application.execution_scope import execution_scope

from tests.prompt_plan_fixture import prompt_plan, runtime_route
from tests.codex_app_server_fixture import app_server_replay

import json
import base64
import time
from dataclasses import replace
from chatcopilot.contracts.execution import TurnExecutionContext, TraceContext, HostRuntimePolicy
from chatcopilot.contracts.model_runtime import ModelSelection
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter as _CodexRuntimeAdapter
from chatcopilot.agent.runtimes.codex_events import CodexJsonlProjector
from chatcopilot.agent.runtimes.registry import runtime_ids, build_runtime_adapter
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.runtime_state import prepare_runtime_deployment
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
    TurnError,
)
from chatcopilot.contracts.runtime_adapter import (
    RUNTIME_IDS,
    RuntimeCapabilityError,
    RuntimeOpenRequest as _RuntimeOpenRequest,
    RuntimeSessionRef,
    CAPABILITY_NATIVE_RESUME,
    CodexMainSessionPolicy,
)
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.core.config import ChatConfig, LLMConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.core.model_credentials import (
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


_active_test_route = runtime_route("codex")


def CodexRuntimeAdapter(**kwargs):
    global _active_test_route
    kwargs.setdefault("route", runtime_route("codex", kwargs["runtime_config"].llm))
    _active_test_route = kwargs["route"]
    return _CodexRuntimeAdapter(**kwargs)


def RuntimeOpenRequest(**kwargs):
    kwargs.setdefault("route", _active_test_route)
    return _RuntimeOpenRequest(**kwargs)


def _dynamic_tool(calls: list[str] | None = None) -> ToolDef:
    def handler(args: dict, _context: ToolContext) -> ToolResult:
        value = str(args.get("value") or "")
        if calls is not None:
            calls.append(value)
        return ToolResult(ok=True, summary=f"dynamic:{value}")

    return ToolDef(
        name="dynamic_echo",
        summary="Echo through the live session executor.",
        input_schema=object_schema(
            {"value": {"type": "string"}},
            required=("value",),
        ),
        output_schema=object_schema(),
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
            "access_token": _access_token(token),
            "refresh_token": token,
            "account_id": "test-account",
        },
        "last_refresh": "2026-07-28T00:00:00Z",
    }


def _access_token(label="test", *, account="test-account"):
    payload = {"exp": time.time() + 3600, "label": label,
               "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return "fixture." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".fixture"


def _runtime_config(routing, auth_root=None):
    return SimpleNamespace(routing=routing, codex_extensions="", codex_extension_env={}, llm=LLMConfig(
        provider="openai", model=routing.code_model, api="chatgpt_responses" if auth_root else "openai_responses",
        base_url="https://chatgpt.com/backend-api/codex" if auth_root else "https://api.openai.com/v1",
        api_key="" if auth_root else "fixture-api-key", auth_mode="chatgpt" if auth_root else "api_key",
        credential_root=str(auth_root or ""), reasoning_effort=routing.code_reasoning_effort))



class BackendRegistryTests(TestCase):
    def test_explanatory_answer_survives_native_integrity_check(self) -> None:
        answer = "我能解释运行框架；没有回执不能声称文件已修改、消息已发送或任务已完成。"
        llm = mock.Mock(model="fixture-model")
        llm.chat.return_value = ChatResult(content=answer)
        backend = build_runtime_adapter(runtime_route(), tool_names=set(), llm=llm, runtime_config=ChatConfig(),
                                tool_executor=ToolExecutor(caller_role_hint="owner", tools=[]), tools_schema=[])
        session = backend.open_session(RuntimeOpenRequest(
            session_id="integrity", prompt_plan=prompt_plan("system"), route=runtime_route()
        ))
        events = []
        result = backend.stream_turn(session, AgentTask("解释框架设计"), on_event=events.append)
        self.assertEqual(result.final_text, answer)
        self.assertTrue(result.response_integrity.ok)
        self.assertEqual(next(event.text for event in events if isinstance(event, FinalText)), answer)
        backend.close_session(session)

    def test_three_main_backends_are_code_registered(self) -> None:
        self.assertEqual(RUNTIME_IDS, ("native", "langgraph", "codex"))
        self.assertEqual(runtime_ids(), frozenset(RUNTIME_IDS))

    def test_unknown_runtime_fails_without_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown Agent runtime"):
            runtime_route("other")

    def test_missing_capability_is_deterministic_and_does_not_fallback(self) -> None:
        backend = build_runtime_adapter(runtime_route(), tool_names=set())
        with self.assertRaises(RuntimeCapabilityError) as caught:
            backend.open_session(
                RuntimeOpenRequest(
                    session_id="sid",
                    prompt_plan=prompt_plan("system"),
                    route=runtime_route(),
                    required_capabilities=frozenset({CAPABILITY_NATIVE_RESUME}),
                )
            )
        self.assertEqual(caught.exception.error_code, "runtime_capability_missing")
        self.assertIn("agents.runtime", str(caught.exception))

    def test_inprocess_factory_binds_each_open_request_and_isolates_messages(self) -> None:
        for runtime_id in ("native", "langgraph"):
            with self.subTest(runtime_id=runtime_id):
                llm = mock.Mock(model="fixture-model")
                llm.chat.return_value = ChatResult(content="completed")
                backend = build_runtime_adapter(
                    runtime_route(runtime_id),
                    tool_names=set(),
                    llm=llm,
                    runtime_config=ChatConfig(),
                    tool_executor=ToolExecutor(caller_role_hint="owner", tools=[]),
                    tools_schema=[],
                )
                first = backend.open_session(RuntimeOpenRequest(
                    session_id="session-first",
                    prompt_plan=prompt_plan("first", runtime_id=runtime_id),
                    route=runtime_route(runtime_id)))
                second = backend.open_session(RuntimeOpenRequest(
                    session_id="session-second",
                    prompt_plan=prompt_plan("second", runtime_id=runtime_id),
                    route=runtime_route(runtime_id)))
                first_session = backend._resolve(first)
                second_session = backend._resolve(second)
                self.assertIsNot(first_session, second_session)
                self.assertEqual(first_session.session_id, "session-first")
                self.assertEqual(second_session.session_id, "session-second")
                second_messages = second_session.snapshot_messages()
                result = backend.stream_turn(first, AgentTask("only first"), on_event=lambda _: None)
                self.assertEqual(result.final_text, "completed")
                self.assertEqual(second_session.snapshot_messages(), second_messages)
                backend.close_session(first)
                with self.assertRaises(KeyError):
                    backend._resolve(first)
                self.assertIs(backend._resolve(second), second_session)
                backend.close_session(second)
                llm.close.assert_not_called()


class CodexBackendResumeTests(TestCase):
    def test_explanatory_answer_survives_codex_integrity_check(self) -> None:
        answer = "我能解释运行框架；没有回执不能声称文件已修改、消息已发送或任务已完成。"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(code_command="codex exec --model {model} --cd {workdir}",
                code_model="gpt-test", code_reasoning_effort="medium", code_timeout_seconds=30,
                code_workdir_env="CHATCOPILOT_TEST_UNUSED_WORKDIR")
            backend = CodexRuntimeAdapter(tool_names=set(), runtime_config=_runtime_config(routing, auth_root), tools=())
            session = backend.open_session(RuntimeOpenRequest(session_id="integrity", prompt_plan=prompt_plan("system"),
                options={"workspace_root": root, "runtime_state_root": root / "state", "role_hint": "owner"}))
            response = subprocess.CompletedProcess(["codex"], 0, "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-integrity"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}),
            ]), "")
            events = []
            with (
                mock.patch.dict(os.environ, {"CHATCOPILOT_CODEX_BOT_HOME": str(auth_root)}),
                mock.patch("chatcopilot.external_tools.codex_cli.command._resolve_executable", return_value="/usr/bin/true"),
                mock.patch("chatcopilot.agent.runtimes.codex.build_codex_subprocess_env", return_value={}),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay([response])),
            ):
                result = backend.stream_turn(session, AgentTask("解释框架设计"), on_event=events.append)
            self.assertEqual(result.final_text, answer)
            self.assertTrue(result.response_integrity.ok)
            self.assertEqual(next(event.text for event in events if isinstance(event, FinalText)), answer)

    def test_main_credential_root_rejects_default_personal_home(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_CODEX_BOT_HOME": str(Path("~").expanduser() / ".codex")},
                clear=True,
            ),
            self.assertRaisesRegex(CredentialError, "auth_root_personal_forbidden"),
        ):
            _CodexRuntimeAdapter._bot_credential_root()

    def test_main_credential_root_rejects_personal_home_descendant(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_CODEX_BOT_HOME": str(Path("~").expanduser() / ".codex" / "bot-auth")},
                clear=True,
            ),
            self.assertRaisesRegex(CredentialError, "auth_root_personal_forbidden"),
        ):
            _CodexRuntimeAdapter._bot_credential_root()

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
            backend = CodexRuntimeAdapter(
                tool_names={"dynamic_echo", "denied"},
                runtime_config=_runtime_config(routing, auth_root),
                tools=(_dynamic_tool(),),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="acp-1",
                    prompt_plan=prompt_plan("system"),
                    allowed_tool_names=frozenset({"dynamic_echo"}),
                    options={
                        "workspace_root": root,
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay([first, second])) as run,
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
            self.assertEqual(resume_command[1], "app-server")
            self.assertEqual(run.call_args_list[1].kwargs["thread_id"], "thread-native-1")
            self.assertEqual(run.call_args_list[0].kwargs["thread_id"], "")
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
            schemas = run.call_args_list[0].kwargs["dynamic_tools"]
            self.assertEqual(schemas[0]["name"], "agentstrata")
            self.assertEqual([tool["name"] for tool in schemas[0]["tools"]], ["dynamic_echo"])
            backend.close_session(native_ref)

    def test_dynamic_tool_receipts_are_emitted_as_agent_events(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(code_model="gpt-test", code_reasoning_effort="medium")
            config = _runtime_config(routing, auth_root)
            calls = []
            tool = _dynamic_tool(calls)
            backend = CodexRuntimeAdapter(tool_names={tool.name}, runtime_config=config, tools=(tool,),
                tool_executor=ToolExecutor(tools=[tool], caller_role_hint="owner"))
            ref = backend.open_session(RuntimeOpenRequest(session_id="session", prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({tool.name}),
                options={"workspace_root": root, "runtime_state_root": root / "state", "role_hint": "owner"}))

            def run(command, **kw):
                kw["on_thread"]("thread")
                reply = kw["on_request"]("item/tool/call", {"threadId": "thread", "namespace": "agentstrata",
                    "tool": "dynamic_echo", "arguments": {"value": "bound"}, "callId": "call-one"})
                self.assertTrue(reply["success"])
                return app_server_replay(subprocess.CompletedProcess(command, 0,
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}), ""))(command, **kw)
            events = []
            with mock.patch("chatcopilot.external_tools.codex_cli.command._resolve_executable", return_value="/usr/bin/true"), \
                 mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=run):
                result = backend.stream_turn(ref, AgentTask("request"), on_event=events.append)
            self.assertEqual(calls, ["bound"])
            self.assertEqual(result.stop_reason, "end_turn")
            receipts = [e for e in events if isinstance(e, ToolFinished)]
            self.assertEqual(len(receipts), 1)
            self.assertTrue(receipts[0].ok)
            self.assertEqual(receipts[0].source, "host")
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
            backend = CodexRuntimeAdapter(
                tool_names=set(),
                runtime_config=_runtime_config(routing, auth_root),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="failing-event-sink",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay(completed)),
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

    def test_codex_transport_failure_does_not_replay_tools(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(code_model="gpt-test", code_reasoning_effort="medium")
            config = _runtime_config(routing, auth_root)
            calls = []
            tool = _dynamic_tool(calls)
            backend = CodexRuntimeAdapter(tool_names={tool.name}, runtime_config=config, tools=(tool,),
                tool_executor=ToolExecutor(tools=[tool], caller_role_hint="owner"))
            ref = backend.open_session(RuntimeOpenRequest(session_id="session", prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({tool.name}),
                options={"workspace_root": root, "runtime_state_root": root / "state", "role_hint": "owner"}))

            def run(command, **kw):
                kw["on_thread"]("thread")
                kw["on_request"]("item/tool/call", {"threadId": "thread", "namespace": "agentstrata",
                    "tool": "dynamic_echo", "arguments": {"value": "once"}})
                raise RuntimeError("transport disconnected")
            events = []
            with mock.patch("chatcopilot.external_tools.codex_cli.command._resolve_executable", return_value="/usr/bin/true"), \
                 mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=run):
                result = backend.stream_turn(ref, AgentTask("request"), on_event=events.append)
            self.assertEqual(calls, ["once"])
            self.assertEqual(result.stop_reason, "runtime_error")
            self.assertEqual(result.failure.stage, "protocol")
            self.assertEqual(backend._resolve(ref).connection, [])
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
            backend = CodexRuntimeAdapter(
                tool_names={"dynamic_echo"},
                runtime_config=_runtime_config(routing, auth_root),
                tools=(_dynamic_tool(),),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="observable-codex",
                    prompt_plan=prompt_plan("system baseline"),
                    allowed_tool_names=frozenset({"dynamic_echo"}),
                    options={
                        "workspace_root": root / "workspace",
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay(completed)),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("inspect context", execution=TurnExecutionContext(trace=TraceContext("trace-request-1", "host:actor"))),
                    on_event=events.append,
                )

            context = next(
                event for event in events if isinstance(event, ContextSnapshotPrepared)
            )
            started = next(event for event in events if isinstance(event, LlmCallStarted))
            finished = next(event for event in events if isinstance(event, LlmCallFinished))
            self.assertLess(events.index(context), events.index(started))
            self.assertEqual(context.runtime_id, "codex")
            self.assertEqual(context.coverage, "adapter_visible")
            self.assertEqual(context.omitted, ("provider_internal_instructions", "provider_native_deferred_tool_loading"))
            self.assertEqual(context.trace_id, "trace-request-1")
            self.assertEqual(context.parent_span_id, "host:actor")
            self.assertEqual(started.parent_span_id, "host:actor")
            self.assertEqual(finished.parent_span_id, "host:actor")
            self.assertEqual(context.snapshot_id, started.context_snapshot_id)
            self.assertEqual(context.snapshot_id, finished.context_snapshot_id)
            self.assertEqual(finished.visible_response["content"], "done")
            self.assertEqual(finished.visible_response["coverage"], "adapter_visible")
            self.assertEqual(finished.visible_response["tool_calls"], [])
            self.assertIn("provider_internal_turns", finished.visible_response["omitted"])
            self.assertEqual(context.session_messages[-1]["role"], "user")
            self.assertIn("inspect context", context.session_messages[-1]["content"])
            self.assertEqual(context.effective_messages[0]["role"], "developer")
            self.assertEqual(context.effective_messages[1]["role"], "user")
            self.assertIn("system baseline", context.effective_messages[1]["content"])
            self.assertIn("inspect context", context.effective_messages[1]["content"])
            self.assertEqual(context.tool_schemas[0]["name"], "agentstrata")
            self.assertEqual(context.tool_schemas[0]["tools"][0]["name"], "dynamic_echo")
            self.assertGreater(context.estimated_tokens, 0)
            self.assertEqual(
                finished.usage,
                {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "reasoning_tokens": 12,
                    "cached_tokens": 40,
                    "cache_write_tokens": 8,
                },
            )
            span_starts = [event for event in events if isinstance(event, SpanStarted)]
            span_finishes = [event for event in events if isinstance(event, SpanFinished)]
            self.assertEqual(
                {event.kind for event in span_finishes},
                {"command", "reasoning", "mcp_tool", "web_search", "file_change", "plan"},
            )
            self.assertEqual([event.kind for event in span_starts], ["command"])
            self.assertTrue(all(event.parent_span_id == started.span_id for event in span_starts))
            portable_events = repr([*span_starts, *span_finishes, finished.visible_response])
            for private_value in (
                "provider-private-reasoning",
            ):
                self.assertNotIn(private_value, portable_events)
            for public_value in ("private-command-output", "private-argument", "private-result",
                                 "private-query", "/private/path", "private plan step"):
                self.assertIn(public_value, portable_events)
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
            backend = CodexRuntimeAdapter(
                tool_names=set(),
                runtime_config=_runtime_config(routing, auth_root),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="resume-context",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay(outputs)),
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
                ("provider_internal_instructions", "provider_native_deferred_tool_loading", "provider_managed_resume_context"),
            )
            self.assertEqual(
                [message["role"] for message in context.session_messages],
                ["user", "assistant", "user"],
            )
            backend.close_session(backend.current_session_ref(ref))

    def test_codex_public_progress_and_final_delivery_remain_separate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(code_model="gpt-test", code_reasoning_effort="medium")
            config = _runtime_config(routing, auth_root)
            calls = []
            tool = _dynamic_tool(calls)
            backend = CodexRuntimeAdapter(tool_names={tool.name}, runtime_config=config, tools=(tool,),
                tool_executor=ToolExecutor(tools=[tool], caller_role_hint="owner"))
            ref = backend.open_session(RuntimeOpenRequest(session_id="session", prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({tool.name}),
                options={"workspace_root": root, "runtime_state_root": root / "state", "role_hint": "owner"}))

            events = []
            def run(command, **kw):
                kw["on_thread"]("thread")
                kw["on_notification"]("turn/started", {"threadId": "thread", "turn": {"id": "fixture-turn"}})
                kw["on_notification"]("item/started", {"threadId": "thread", "turnId": "fixture-turn",
                    "item": {"id": "message", "type": "agentMessage", "phase": "commentary", "text": "working"}})
                self.assertFalse(any(isinstance(e, FinalText) for e in events))
                return app_server_replay(subprocess.CompletedProcess(command, 0,
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}), ""))(command, **kw)
            with mock.patch("chatcopilot.external_tools.codex_cli.command._resolve_executable", return_value="/usr/bin/true"), \
                 mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=run):
                result = backend.stream_turn(ref, AgentTask("request"), on_event=events.append)
            self.assertEqual(result.final_text, "done")
            self.assertEqual(sum(isinstance(e, FinalText) for e in events), 1)
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
            backend = CodexRuntimeAdapter(
                tool_names=set(),
                runtime_config=_runtime_config(routing, auth_root),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="oversized-jsonl",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=RuntimeError("App Server protocol record exceeds limit")),
            ):
                result = backend.stream_turn(
                    ref,
                    AgentTask("oversized output"),
                    on_event=events.append,
                )

            error = next(event for event in events if isinstance(event, TurnError))
            self.assertEqual(result.stop_reason, "runtime_error")
            self.assertIn("protocol record exceeds limit", error.message)
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
        self.assertEqual(item_finish.data["error"]["message"], "provider detail")

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
        self.assertEqual(len(projector.final_text), 1200 * 1024 + 1)

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

    def test_typed_turn_selection_does_not_mutate_runtime_default(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(code_model="gpt-test", code_reasoning_effort="medium")
            config = _runtime_config(routing, auth_root)
            calls = []
            tool = _dynamic_tool(calls)
            backend = CodexRuntimeAdapter(tool_names={tool.name}, runtime_config=config, tools=(tool,),
                tool_executor=ToolExecutor(tools=[tool], caller_role_hint="owner"))
            ref = backend.open_session(RuntimeOpenRequest(session_id="session", prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({tool.name}),
                options={"workspace_root": root, "runtime_state_root": root / "state", "role_hint": "owner"}))

            chosen = ModelSelection(replace(config.llm.model_route(), model="selected", reasoning_effort="high"),
                                    scope="once", source="profile", profile="test")
            observed = []
            def run(command, **kw):
                observed.append((kw["model"], kw["effort"]))
                return app_server_replay(subprocess.CompletedProcess(command, 0,
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}), ""))(command, **kw)
            with mock.patch("chatcopilot.external_tools.codex_cli.command._resolve_executable", return_value="/usr/bin/true"), \
                 mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=run):
                backend.stream_turn(ref, AgentTask("request", execution=TurnExecutionContext(model_selection=chosen)), on_event=lambda _: None)
                backend.stream_turn(ref, AgentTask("request"), on_event=lambda _: None)
            self.assertEqual(observed, [("selected", "high"), ("gpt-test", "medium")])
            self.assertEqual(config.llm.model, "gpt-test")
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
                "runtime_config": _runtime_config(routing, auth_root),
                "tools": (_dynamic_tool(),),
            }
            request = RuntimeOpenRequest(
                session_id="acp-persisted",
                prompt_plan=prompt_plan("system", runtime_id="codex"),
                route=runtime_route("codex", kwargs["runtime_config"].llm),
                allowed_tool_names=frozenset({"dynamic_echo"}),
                options={
                    "workspace_root": root,
                    "runtime_state_root": root / "state",
                },
            )
            backend = CodexRuntimeAdapter(**kwargs)
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay(completed)),
            ):
                backend.stream_turn(ref, AgentTask("one"), on_event=lambda _: None)
            native_ref = backend.current_session_ref(ref)
            backend.close_session(native_ref)

            reconstructed = CodexRuntimeAdapter(**kwargs)
            restored_ref = reconstructed.open_session(request)
            self.assertEqual(restored_ref.value, "persisted-native-id")
            with mock.patch(
                "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                return_value="/usr/bin/true",
            ):
                command = reconstructed._command(reconstructed._resolve(restored_ref))
            self.assertEqual(command[1], "app-server")
            self.assertEqual(reconstructed._resolve(restored_ref).native_session_id, "persisted-native-id")
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
                "runtime_config": _runtime_config(routing, auth_root),
                "tools": (_dynamic_tool(),),
            }
            request = RuntimeOpenRequest(
                session_id="acp-no-persisted-resume",
                prompt_plan=prompt_plan("system", runtime_id="codex"),
                route=runtime_route("codex", kwargs["runtime_config"].llm),
                allowed_tool_names=frozenset({"dynamic_echo"}),
                options={
                    "workspace_root": root,
                    "runtime_state_root": root / "state",
                    "restore_persisted_native_session": False,
                },
            )
            backend = CodexRuntimeAdapter(**kwargs)
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
                    return_value="/usr/bin/true",
                ),
                mock.patch(
                    "chatcopilot.agent.runtimes.codex.build_codex_subprocess_env",
                    return_value={},
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay(completed)),
            ):
                backend.stream_turn(ref, AgentTask("one"), on_event=lambda _: None)
            backend.close_session(backend.current_session_ref(ref))

            reconstructed = CodexRuntimeAdapter(**kwargs)
            fresh_ref = reconstructed.open_session(request)
            fresh_state = reconstructed._resolve(fresh_ref)
            self.assertEqual(fresh_state.native_session_id, "")
            with mock.patch(
                "chatcopilot.external_tools.codex_cli.command._resolve_executable",
                return_value="/usr/bin/true",
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
                "runtime_config": _runtime_config(routing),
            }
            user_request = RuntimeOpenRequest(
                session_id="role-change",
                prompt_plan=prompt_plan("system"),
                route=runtime_route("codex", kwargs["runtime_config"].llm),
                options={
                    "workspace_root": root,
                    "runtime_state_root": root / "state",
                    "role_hint": "user",
                },
            )
            backend = CodexRuntimeAdapter(**kwargs)
            ref = backend.open_session(user_request)
            state = backend._resolve(ref)
            state.native_session_id = "old-elevated-thread"
            backend._persist_session_state(state)
            backend.close_session(ref)

            owner_request = RuntimeOpenRequest(
                session_id="role-change",
                prompt_plan=prompt_plan("system"),
                route=runtime_route("codex", kwargs["runtime_config"].llm),
                options={
                    "workspace_root": root,
                    "runtime_state_root": root / "state",
                    "role_hint": "admin",
                },
            )
            reconstructed = CodexRuntimeAdapter(**kwargs)
            restored_ref = reconstructed.open_session(owner_request)

            self.assertNotEqual(restored_ref.value, "old-elevated-thread")
            self.assertEqual(
                reconstructed._resolve(restored_ref).native_session_id,
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
                "runtime_config": _runtime_config(routing),
            }
            request = RuntimeOpenRequest(
                session_id="identity-change",
                prompt_plan=prompt_plan("system"),
                route=runtime_route("codex", kwargs["runtime_config"].llm),
                caller_identity=SessionIdentity(user_id="caller-a"),
                options={
                    "workspace_root": root,
                    "runtime_state_root": root / "state",
                },
            )
            backend = CodexRuntimeAdapter(**kwargs)
            ref = backend.open_session(request)
            state = backend._resolve(ref)
            state.native_session_id = "old-caller-thread"
            backend._persist_session_state(state)
            backend.close_session(ref)

            reconstructed = CodexRuntimeAdapter(**kwargs)
            restored_ref = reconstructed.open_session(
                RuntimeOpenRequest(
                    session_id="identity-change",
                    prompt_plan=prompt_plan("system"),
                    caller_identity=SessionIdentity(user_id="caller-b"),
                    options={
                        "workspace_root": root,
                        "runtime_state_root": root / "state",
                    },
                )
            )

            self.assertNotEqual(restored_ref.value, "old-caller-thread")
            self.assertEqual(
                reconstructed._resolve(restored_ref).native_session_id,
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
            backend = CodexRuntimeAdapter(
                tool_names=set(),
                runtime_config=_runtime_config(routing, auth_root),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="generation-change",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay([first, second])) as run,
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
            state = backend._resolve(RuntimeSessionRef("codex", "new-account-thread"))
            self.assertEqual(state.credential_generation, 2)
            self.assertEqual(state.native_session_id, "new-account-thread")
            backend.close_session(RuntimeSessionRef("codex", "new-account-thread"))

    def test_subscription_tokens_are_handed_off_without_runtime_auth_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = _main_auth_root(root)
            routing = SimpleNamespace(code_model="gpt-test", code_reasoning_effort="medium")
            config = _runtime_config(routing, auth_root)
            calls = []
            tool = _dynamic_tool(calls)
            backend = CodexRuntimeAdapter(tool_names={tool.name}, runtime_config=config, tools=(tool,),
                tool_executor=ToolExecutor(tools=[tool], caller_role_hint="owner"))
            ref = backend.open_session(RuntimeOpenRequest(session_id="session", prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({tool.name}),
                options={"workspace_root": root, "runtime_state_root": root / "state", "role_hint": "owner"}))

            def run(command, **kw):
                self.assertEqual(kw["authentication"]["type"], "chatgptAuthTokens")
                self.assertNotIn("refreshToken", kw["authentication"])
                self.assertFalse((backend._resolve(ref).codex_home / "auth.json").exists())
                return app_server_replay(subprocess.CompletedProcess(command, 1, "", "401 Unauthorized fixture"))(command, **kw)
            with mock.patch("chatcopilot.external_tools.codex_cli.command._resolve_executable", return_value="/usr/bin/true"), \
                 mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=run):
                result = backend.stream_turn(ref, AgentTask("request"), on_event=lambda _: None)
            self.assertNotIn("401", result.final_text)
            self.assertEqual(result.stop_reason, "llm_error")
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
            backend = CodexRuntimeAdapter(
                tool_names=set(),
                runtime_config=_runtime_config(routing, auth_root),
            )
            ref = backend.open_session(
                RuntimeOpenRequest(
                    session_id="empty-success",
                    prompt_plan=prompt_plan("system"),
                    options={
                        "workspace_root": root / "workspace",
                        "runtime_state_root": root / "state",
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
                    return_value="/usr/bin/true",
                ),
                mock.patch("chatcopilot.agent.runtimes.codex.run_app_server", side_effect=app_server_replay(completed)),
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
        backend = CodexRuntimeAdapter(
            tool_names={"dynamic_echo"},
            runtime_config=_runtime_config(self._routing()),
            tools=(_dynamic_tool(),),
            runtime_policy=policy,
        )
        ref = backend.open_session(
            RuntimeOpenRequest(
                session_id="policy-session",
                prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({"dynamic_echo"}),
                caller_identity=(
                    SessionIdentity(user_id=caller_user_id) if caller_user_id is not None else None
                ),
                options={
                    "workspace_root": root,
                    "source_root": root,
                    "runtime_state_root": root / "state",
                    "role_hint": role_hint,
                    "execution_scope": execution_scope(
                        role_hint, root, (root,) if role_hint == "owner" else ()
                    ),
                },
                host_policy=HostRuntimePolicy(network_access=True,
                    native_capabilities=frozenset({"web_search", "image_generation"})),
            )
        )
        state = backend._resolve(ref)
        backend._prepare_app_server_home(state)
        with mock.patch(
            "chatcopilot.external_tools.codex_cli.command._resolve_executable",
            return_value="/usr/bin/true",
        ):
            command = backend._command(state)
        from chatcopilot.agent.context.prompt_plan import render_codex_developer
        prompt = render_codex_developer(state.prompt_plan, execution_policy=backend._execution_policy_prompt(state))
        prompt += backend._prompt(state, AgentTask("do work"))
        backend.close_session(ref)
        return command, prompt

    def _policy_fingerprint(self, policy: CodexMainSessionPolicy) -> str:
        backend = CodexRuntimeAdapter(
            tool_names=set(),
            runtime_config=_runtime_config(self._routing()),
            runtime_policy=policy,
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

        self.assertIn('default_permissions="agentstrata"', command)
        self.assertIn("--strict-config", command)
        self.assertTrue(any(str(root) in item and "permissions.agentstrata.filesystem" in item for item in command))
        self.assertTrue(any(f'HOME = "{root}"' in item for item in command))
        self.assertIn("permissions.agentstrata.network.enabled=true", command)
        self.assertIn('web_search="live"', command)
        self.assertIn("project_doc_max_bytes=0", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("features.default_mode_request_user_input=false", command)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertIn("current-conversation ordinary files", prompt)

    def test_owner_scope_uses_native_permissions_without_session_gateway(self):
        with TemporaryDirectory() as tmp:
            command, prompt = self._command_and_prompt(Path(tmp), CodexMainSessionPolicy(), role_hint="owner")
        self.assertIn('default_permissions="agentstrata"', command)
        self.assertIn("--strict-config", command)
        self.assertFalse(any("mcp_servers.chatcopilot" in item for item in command))
        self.assertIn("configured instance and project resources", prompt)

    def test_eval_confinement_disables_command_network_and_web_search(self) -> None:
        policy = CodexMainSessionPolicy(
            network_access=False,
            web_search_mode="disabled",
            sandbox_mode="read-only",
            image_generation=False,
        )
        with TemporaryDirectory() as tmp:
            command, prompt = self._command_and_prompt(Path(tmp), policy)

        self.assertTrue(any('"read"' in item and "permissions.agentstrata.filesystem" in item for item in command))
        self.assertIn("permissions.agentstrata.network.enabled=false", command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("features.image_generation=false", command)
        self.assertNotIn("features.network_proxy.enabled=true", command)
        self.assertNotIn('features.network_proxy.domains={ "*" = "allow" }', command)
        self.assertIn("web search is disabled", prompt)

    def test_command_confinement_changes_policy_fingerprint(self) -> None:
        baseline = self._policy_fingerprint(CodexMainSessionPolicy())

        variants = (
            CodexMainSessionPolicy(network_access=False),
            CodexMainSessionPolicy(web_search_mode="disabled"),
            CodexMainSessionPolicy(sandbox_mode="read-only"),
            CodexMainSessionPolicy(image_generation=False),
        )

        for policy in variants:
            with self.subTest(policy=policy):
                self.assertNotEqual(self._policy_fingerprint(policy), baseline)

    def test_eval_tool_surface_policy_requires_strict_booleans(self) -> None:
        for field_name in (
            "allow_delegate_tools",
            "allow_unified_search_tool",
            "image_generation",
        ):
            with (
                self.subTest(field=field_name),
                self.assertRaisesRegex(
                    TypeError,
                    field_name,
                ),
            ):
                CodexMainSessionPolicy(**{field_name: 1})

    def test_explicit_evaluation_sandbox_is_respected(self) -> None:
        with TemporaryDirectory() as tmp:
            command, _ = self._command_and_prompt(
                Path(tmp), CodexMainSessionPolicy(sandbox_mode="read-only"), role_hint="owner"
            )
        self.assertTrue(any('"read"' in item and "permissions.agentstrata.filesystem" in item for item in command))


class BackendStateTransitionTests(TestCase):
    def test_switch_deletes_old_state_before_target_start_and_never_restores(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            prepare_runtime_deployment(
                instance_id="demo", target_runtime_id="native", workspace_root=root
            )
            transcript = root / "p2p_user" / "transcripts"
            runtime_state = root / "p2p_user" / ".runtime-sessions"
            group_runtime_state = (
                root
                / "group_demo"
                / ".conversation-state"
                / "runtime-sessions"
                / "actor-digest"
            )
            group_journal = (
                root
                / "group_demo"
                / ".conversation-state"
                / "group-conversation.jsonl"
            )
            transcript.mkdir(parents=True)
            runtime_state.mkdir(parents=True)
            group_runtime_state.mkdir(parents=True)
            group_journal.write_text("shared history", encoding="utf-8")
            (transcript / "turn.jsonl").write_text("history", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "explicit.*runtime-migrate"):
                prepare_runtime_deployment(
                    instance_id="demo", target_runtime_id="codex", workspace_root=root
                )
            self.assertTrue(transcript.exists())
            self.assertTrue(runtime_state.exists())
            self.assertTrue(group_runtime_state.exists())
            self.assertTrue(group_journal.is_file())
            marker = json.loads((root / ".agent-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["runtime_id"], "native")

    def test_unchanged_backend_preserves_histories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            prepare_runtime_deployment(
                instance_id="demo", target_runtime_id="native", workspace_root=root
            )
            transcript = root / "user" / "transcripts"
            transcript.mkdir(parents=True)
            (transcript / "turn.jsonl").write_text("history", encoding="utf-8")
            transition = prepare_runtime_deployment(
                instance_id="demo", target_runtime_id="native", workspace_root=root
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
