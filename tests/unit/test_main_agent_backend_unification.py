from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from chatcopilot.agent.backends.codex import CodexAgentBackend
from chatcopilot.agent.backends.registry import backend_ids, build_backend
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.tools.executor import ToolExecutor, ToolResult
from chatcopilot.botspec.backend_state import prepare_backend_deployment
from chatcopilot.contracts.agent import (
    AgentTask,
    FinalText,
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
                    system_baseline="system",
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
                    system_baseline="system",
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
                result1 = backend.stream_turn(ref, AgentTask("one"), on_event=lambda _: None)
                native_ref = backend.current_session_ref(ref)
                result2 = backend.stream_turn(native_ref, AgentTask("two"), on_event=lambda _: None)

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
            gateway = json.loads(
                backend.native_session(native_ref).gateway_config.read_text(encoding="utf-8")
            )
            self.assertEqual(gateway["allowed_tools"], ["dynamic_echo"])
            backend.close_session(native_ref)

    def test_session_relay_tool_receipts_are_emitted_as_agent_events(self) -> None:
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
                    session_id="relay-evidence",
                    system_baseline="system",
                    allowed_tool_names=frozenset({"dynamic_echo"}),
                    options={
                        "workspace_root": root,
                        "backend_state_root": root / "state",
                        "role_hint": "owner",
                    },
                )
            )
            events: list[object] = []

            def run_with_relay(*_args, **_kwargs):
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
                result = backend.stream_turn(ref, AgentTask("use the tool"), on_event=events.append)

            started = next(event for event in events if isinstance(event, ToolStarted))
            finished = next(event for event in events if isinstance(event, ToolFinished))
            self.assertEqual(started.name, "dynamic_echo")
            self.assertEqual(started.arguments, {"value": "bridge-evidence"})
            self.assertEqual(started.trace_id, finished.trace_id)
            self.assertTrue(finished.ok)
            self.assertEqual(finished.data["summary"], "dynamic:bridge-evidence")
            self.assertEqual(result.final_text, "done")
            backend.close_session(ref)

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
                    system_baseline="system",
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
                system_baseline="system",
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
                system_baseline="system",
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
                system_baseline="system",
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
                system_baseline="system",
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
                system_baseline="system",
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
                    system_baseline="system",
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
                    system_baseline="system",
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
                    system_baseline="system",
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
                    system_baseline="system",
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
                system_baseline="system",
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
                system_baseline="system",
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
