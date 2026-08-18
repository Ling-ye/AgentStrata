"""Codex CLI main-agent backend with native resume and a scoped MCP gateway."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chatcopilot.agent.context import (
    frame_task_message,
    validated_image_resource_receipts,
)
from chatcopilot.contracts.agent import (
    AgentResult,
    AgentTask,
    EventSink,
    FinalText,
    InputResourcesDispatched,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnError,
)
from chatcopilot.contracts.agent_backend import (
    BackendCapabilities,
    BackendOpenRequest,
    BackendSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_NATIVE_RESUME,
    CAPABILITY_REPOSITORY_MUTATION,
    CAPABILITY_TOOLS,
    CodexMainSessionPolicy,
    CODEX_ACCESS_MODES,
    CODEX_ACCESS_WORKTREE,
    require_backend_capabilities,
)
from chatcopilot.contracts.model_selection import CodeModelSelection
from chatcopilot.core.image_content import (
    SUPPORTED_IMAGE_MEDIA_TYPES,
    normalize_image_media_type,
    validate_image_file,
)
from chatcopilot.core.model_selection import (
    CODE_MODEL_SELECTION_METADATA_KEY,
    default_code_model_selection,
    validate_frozen_code_model_selection,
)
from chatcopilot.external_tools.codex_cli.command import (
    build_codex_command,
    build_codex_subprocess_env,
)
from chatcopilot.external_tools.codex_cli.credentials import (
    CredentialError,
    credential_lease,
    validate_auth_root_path,
)
from chatcopilot.external_tools.codex_cli.process_runner import run_codex_process
from chatcopilot.external_tools.codex_cli import session_gateway as _standalone_gateway
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.backends.session_relay import SessionToolRelay

if TYPE_CHECKING:
    from chatcopilot.agent.session import ToolPayloadFilter


@dataclass
class _CodexSession:
    acp_session_id: str
    system_baseline: str
    allowed_tool_names: frozenset[str]
    gateway_config: Path
    audit_path: Path
    state_root: Path
    workdir: Path
    codex_home: Path
    session_state_path: Path
    relay: SessionToolRelay
    role_hint: str
    access_mode: str
    policy_fingerprint: str
    isolate_backend_state: bool = False
    native_session_id: str = ""
    credential_generation: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


_ISOLATED_GATEWAY_CONFIG = "/run/chatcopilot-gateway.json"
_ISOLATED_CODEX_HOME = "/sandbox-home/agent/.codex"
_ISOLATED_GATEWAY_VENV = "/opt/chatcopilot-gateway-venv"
_ISOLATED_GATEWAY_SCRIPT = "/opt/chatcopilot-gateway/session_gateway.py"
_ISOLATED_CODEX_BINARY = "/opt/chatcopilot-codex/codex"
_BWRAP_PROBED: set[str] = set()
_ISOLATED_DISABLED_FEATURES = (
    "apps",
    "artifact",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "chronicle",
    "code_mode",
    "code_mode_buffered_exec",
    "code_mode_host",
    "code_mode_only",
    "computer_use",
    "deferred_executor",
    "deferred_tool_world_state",
    "enable_mcp_apps",
    "external_agent_memory_import",
    "goals",
    "guardian_approval",
    "guardianv2",
    "hooks",
    "image_generation",
    "in_app_browser",
    "in_app_updates",
    "js_repl",
    "js_repl_tools_only",
    "memories",
    "mentions_v2",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "plugin_sharing",
    "plugins",
    "recommended_plugins",
    "remote_plugin",
    "request_permissions_tool",
    "shell_snapshot",
    "shell_tool",
    "shell_zsh_fork",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "unified_exec_zsh_fork",
    "workspace_dependencies",
)


class CodexAgentBackend:
    backend_id = "codex"

    def __init__(
        self,
        *,
        tool_names: set[str],
        runtime_config: Any,
        tools: tuple[Any, ...] = (),
        tool_executor: ToolExecutor | None = None,
        tool_payload_filter: ToolPayloadFilter | None = None,
        backend_policy: CodexMainSessionPolicy | None = None,
        **_: Any,
    ) -> None:
        self._runtime_config = runtime_config
        self._tool_names = frozenset(tool_names)
        self._tools = tuple(tools)
        self._tool_executor = tool_executor
        self._tool_payload_filter = tool_payload_filter
        self._policy = backend_policy or CodexMainSessionPolicy()
        self._capabilities = BackendCapabilities(
            names=frozenset(
                {
                    CAPABILITY_CHAT,
                    CAPABILITY_TOOLS,
                    CAPABILITY_NATIVE_RESUME,
                    CAPABILITY_REPOSITORY_MUTATION,
                }
            ),
            tool_names=self._tool_names,
        )
        self._sessions: dict[str, _CodexSession] = {}
        self._aliases: dict[str, str] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def open_session(self, request: BackendOpenRequest) -> BackendSessionRef:
        require_backend_capabilities(
            self.backend_id, self.capabilities, request.required_capabilities
        )
        session_key = hashlib.sha256(request.session_id.encode("utf-8")).hexdigest()[:24]
        stable_id = f"acp-{session_key}"
        options = dict(request.options)
        role_hint = str(options.get("role_hint") or "user").strip().lower()
        access_mode = self._policy.access_for_role(role_hint)
        if access_mode not in CODEX_ACCESS_MODES:
            raise ValueError(f"unsupported Codex access mode: {access_mode}")
        caller_user_id = (
            str(request.caller_identity.user_id or "").strip()
            if request.caller_identity is not None
            else ""
        )
        policy_fingerprint = self._policy_fingerprint(
            role_hint,
            access_mode,
            caller_user_id=caller_user_id,
        )
        existing = self._sessions.get(stable_id)
        if existing is not None:
            if existing.policy_fingerprint == policy_fingerprint:
                return self.current_session_ref(BackendSessionRef(self.backend_id, stable_id))
            self.close_session(BackendSessionRef(self.backend_id, stable_id))
        if access_mode == CODEX_ACCESS_WORKTREE:
            workdir = self._resolve_source_workdir(options.get("source_root"))
        else:
            workdir = self._resolve_workspace_workdir(options.get("workspace_root"))
        state_root = (
            Path(options.get("backend_state_root") or workdir / ".chatcopilot" / "backend-sessions")
            .expanduser()
            .resolve()
        )
        isolate_backend_state = bool(options.get("isolate_backend_state"))
        if isolate_backend_state:
            self._require_isolated_main_codex_sandbox()
        state_root.mkdir(parents=True, exist_ok=True)
        try:
            state_root.chmod(0o700)
        except OSError:
            pass
        if isolate_backend_state:
            self._validate_isolated_roots(workdir=workdir, state_root=state_root)
        gateway_config = state_root / f"{stable_id}.gateway.json"
        audit_root = state_root / "audit"
        audit_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        audit_root.chmod(0o700)
        audit_path = audit_root / f"{stable_id}.audit.jsonl"
        session_state_path = state_root / f"{stable_id}.session.json"
        if bool(options.get("restore_persisted_native_session", True)):
            native_session_id, credential_generation = self._load_native_session_state(
                session_state_path,
                acp_session_id=request.session_id,
                policy_fingerprint=policy_fingerprint,
            )
        else:
            native_session_id, credential_generation = "", 0
        allowed_tool_names = request.allowed_tool_names & self._tool_names
        selected_tools = tuple(tool for tool in self._tools if tool.name in allowed_tool_names)
        executor = self._tool_executor or ToolExecutor(
            tools=list(selected_tools),
            caller_role_hint=role_hint,
        )
        relay = SessionToolRelay(
            tools=selected_tools,
            executor=executor,
            payload_filter=self._tool_payload_filter,
        )
        relay_endpoint = relay.start()
        payload = {
            "schema_version": 1,
            "session_id": request.session_id,
            "allowed_tools": sorted(tool.name for tool in selected_tools),
            "role_hint": role_hint,
            "access_mode": access_mode,
            "policy_fingerprint": policy_fingerprint,
            # The host relay is the authoritative group tool-event recorder.
            # Do not expose a writable audit file inside the model namespace.
            "audit_path": "" if isolate_backend_state else str(audit_path),
            "relay": relay_endpoint.to_dict(),
            "relay_timeout_seconds": self._runtime_config.routing.code_timeout_seconds,
        }
        self._write_json_atomic(gateway_config, payload)
        try:
            gateway_config.chmod(0o600)
        except OSError:
            pass
        codex_home = state_root / f"{stable_id}.codex-home"
        session = _CodexSession(
            acp_session_id=request.session_id,
            system_baseline=request.system_baseline,
            allowed_tool_names=frozenset(tool.name for tool in selected_tools),
            gateway_config=gateway_config,
            audit_path=audit_path,
            state_root=state_root,
            workdir=workdir,
            codex_home=codex_home,
            session_state_path=session_state_path,
            relay=relay,
            role_hint=role_hint,
            access_mode=access_mode,
            policy_fingerprint=policy_fingerprint,
            isolate_backend_state=isolate_backend_state,
            native_session_id=native_session_id,
            credential_generation=credential_generation,
        )
        self._sessions[stable_id] = session
        self._aliases[stable_id] = stable_id
        if native_session_id:
            self._aliases[native_session_id] = stable_id
        self._persist_session_state(session)
        return self.current_session_ref(BackendSessionRef(self.backend_id, stable_id))

    def stream_turn(
        self,
        session: BackendSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
    ) -> AgentResult:
        state = self._resolve(session)
        buffered_events: list[Any] = []
        try:
            with credential_lease(
                self._bot_credential_root(),
                "main",
                state.codex_home,
            ) as lease:
                self._sync_credential_generation(state, lease.generation)
                result = self._stream_turn(
                    state,
                    task,
                    on_event=buffered_events.append,
                )
        except CredentialError as exc:
            self._clear_native_session(state)
            diagnostic = "\n".join(
                event.message
                for event in buffered_events
                if isinstance(event, TurnError) and event.message
            )
            return self._credential_failure_result(
                state,
                task,
                on_event=on_event,
                code=exc.code,
                diagnostic=diagnostic,
            )

        for event in buffered_events:
            on_event(event)
        return result

    def _stream_turn(
        self,
        state: _CodexSession,
        task: AgentTask,
        *,
        on_event: EventSink,
    ) -> AgentResult:
        framed_task = frame_task_message(task)
        state.messages.append({"role": "user", "content": framed_task})
        stale_tool_events = state.relay.drain_tool_events()
        if stale_tool_events:
            raise RuntimeError("Codex session relay retained tool evidence from a prior turn")
        try:
            selection = validate_frozen_code_model_selection(
                self._runtime_config.routing,
                task.metadata.get(CODE_MODEL_SELECTION_METADATA_KEY),
            )
        except (TypeError, ValueError) as exc:
            detail = f"Invalid Codex model selection: {exc}"[-4000:]
            message = (
                "The configured Codex model selection is invalid. "
                "Reset it with `/model code default` or ask the operator to fix "
                "the BotSpec profile."
            )
            on_event(TurnError(code="invalid_model_selection", message=detail))
            on_event(FinalText(message))
            state.messages.append({"role": "assistant", "content": message})
            return AgentResult(
                final_text=message,
                stop_reason="llm_error",
                message_count=len(state.messages),
            )
        try:
            image_paths = self._image_paths(task)
            command = self._command(
                state,
                selection=selection,
                image_paths=image_paths,
            )
            completed = run_codex_process(
                command,
                cwd=state.workdir,
                prompt=self._prompt(state, task),
                timeout_seconds=self._runtime_config.routing.code_timeout_seconds,
                env=self._subprocess_env(state, command[0]),
            )
            image_receipts = validated_image_resource_receipts(task)
        except Exception as exc:  # noqa: BLE001
            audit_error = self._emit_relay_tool_events(state, on_event)
            detail = f"Codex backend failed: {type(exc).__name__}: {exc}"
            if audit_error:
                detail = f"{detail}; relay audit failed: {audit_error}"
            message = self._safe_cli_failure(detail)
            error_code = (
                "codex_auth_invalid" if self._is_auth_failure(detail) else "codex_backend_failed"
            )
            on_event(TurnError(code=error_code, message=detail[-4000:]))
            on_event(FinalText(message))
            state.messages.append({"role": "assistant", "content": message})
            return AgentResult(
                final_text=message,
                stop_reason="llm_error",
                message_count=len(state.messages),
            )

        audit_error = self._emit_relay_tool_events(state, on_event)
        if audit_error:
            detail = f"Codex relay audit failed: {audit_error}"
            on_event(TurnError(code="codex_tool_audit_failed", message=detail[-4000:]))
            final_text = "The Codex tool evidence channel failed; task success is unverified."
            state.messages.append({"role": "assistant", "content": final_text})
            on_event(FinalText(final_text))
            return AgentResult(
                final_text=final_text,
                stop_reason="llm_error",
                message_count=len(state.messages),
            )

        if image_receipts:
            raw_turn = task.metadata.get("eval_turn", 0)
            turn_index = raw_turn if isinstance(raw_turn, int) and raw_turn >= 0 else 0
            on_event(
                InputResourcesDispatched(
                    backend="codex",
                    turn_index=turn_index,
                    request_id=hashlib.sha256(
                        (state.acp_session_id + "\0" + str(len(state.messages))).encode("utf-8")
                    ).hexdigest()[:32],
                    resources=image_receipts,
                )
            )

        final_text = self._consume_events(
            state,
            completed.stdout,
            on_event,
            emit_text=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or final_text or "Codex CLI failed").strip()[-4000:]
            auth_failed = self._is_auth_failure(detail)
            on_event(
                TurnError(
                    code="codex_auth_invalid" if auth_failed else "codex_cli_failed",
                    message=detail,
                )
            )
            if auth_failed:
                final_text = self._auth_remediation()
            elif not final_text:
                final_text = self._generic_cli_failure()
        if not final_text:
            final_text = "Codex completed without a final message."
        state.messages.append({"role": "assistant", "content": final_text})
        on_event(TextDelta(final_text))
        on_event(FinalText(final_text))
        return AgentResult(
            final_text=final_text,
            stop_reason="end_turn" if completed.returncode == 0 else "llm_error",
            message_count=len(state.messages),
        )

    @staticmethod
    def _emit_relay_tool_events(state: _CodexSession, on_event: EventSink) -> str:
        """Project trusted in-process relay receipts onto the shared Agent event protocol."""

        try:
            events = state.relay.drain_tool_events()
        except Exception as exc:  # noqa: BLE001 - evidence failure is returned fail-closed
            return f"{type(exc).__name__}: {exc}"
        for event in events:
            call_id = str(event.get("call_id") or "").strip()
            name = str(event.get("name") or "").strip()
            event_type = str(event.get("type") or "")
            if not call_id or not name:
                return "relay returned an event without call identity"
            if event_type == "tool_started":
                arguments = event.get("arguments")
                if not isinstance(arguments, dict):
                    return "relay returned malformed tool arguments"
                on_event(
                    ToolStarted(
                        name=name,
                        arguments=dict(arguments),
                        trace_id=call_id,
                        span_id=call_id,
                    )
                )
                continue
            if event_type != "tool_finished":
                return f"relay returned unknown tool event {event_type!r}"
            data = event.get("data")
            if data is not None and not isinstance(data, dict):
                return "relay returned malformed tool result data"
            ok = event.get("ok") is True
            on_event(
                ToolFinished(
                    name=name,
                    ok=ok,
                    summary=str(event.get("summary") or ""),
                    error=None if ok else str(event.get("error") or "tool execution failed"),
                    trace_id=call_id,
                    span_id=call_id,
                    data=dict(data) if isinstance(data, dict) else None,
                )
            )
        return ""

    def close_session(self, session: BackendSessionRef) -> None:
        stable = self._stable_key(session)
        state = self._sessions.pop(stable, None)
        for alias, target in tuple(self._aliases.items()):
            if target == stable:
                self._aliases.pop(alias, None)
        if state is not None:
            state.relay.close()
            state.gateway_config.unlink(missing_ok=True)

    def discard_session(self, session: BackendSessionRef) -> None:
        """Invalidate native resume before closing a consistency-poisoned session."""

        state = self._resolve(session)
        self._clear_native_session(state)
        self.close_session(session)

    def current_session_ref(self, session: BackendSessionRef) -> BackendSessionRef:
        state = self._resolve(session)
        value = state.native_session_id or self._stable_key(session)
        return BackendSessionRef(self.backend_id, value)

    def set_system_baseline(self, session: BackendSessionRef, baseline: str) -> None:
        self._resolve(session).system_baseline = baseline

    def record_exchange(
        self, session: BackendSessionRef, user_text: str, assistant_text: str
    ) -> None:
        state = self._resolve(session)
        state.messages.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )

    def snapshot_messages(self, session: BackendSessionRef) -> list[dict[str, Any]]:
        return [dict(item) for item in self._resolve(session).messages]

    def native_session(self, session: BackendSessionRef) -> _CodexSession:
        return self._resolve(session)

    def _command(
        self,
        state: _CodexSession,
        *,
        selection: CodeModelSelection | None = None,
        image_paths: tuple[str, ...] = (),
    ) -> list[str]:
        routing = self._runtime_config.routing
        effective_selection = selection or default_code_model_selection(routing)
        isolate_backend_state = bool(
            getattr(state, "isolate_backend_state", False)
        )
        if isolate_backend_state:
            gateway_command = _ISOLATED_GATEWAY_VENV + "/bin/python"
            gateway_argv = [
                _ISOLATED_GATEWAY_SCRIPT,
                _ISOLATED_GATEWAY_CONFIG,
            ]
        else:
            gateway_command = sys.executable
            gateway_argv = [
                "-m",
                "chatcopilot",
                "mcp-session-gateway",
                str(state.gateway_config),
            ]
        gateway_args = json.dumps(gateway_argv, ensure_ascii=False)
        worktree_access = state.access_mode == CODEX_ACCESS_WORKTREE
        default_sandbox_mode = "read-only" if worktree_access else "workspace-write"
        # Shared-group mutations must cross the actor-bound MCP relay, where
        # workspace containment and payload policy are enforced. Codex builtin
        # tools (including apply_patch, which cannot currently be disabled) get
        # an OS read-only view instead of direct writes to the shared tree.
        sandbox_mode = (
            "read-only"
            if isolate_backend_state
            else (self._policy.sandbox_mode or default_sandbox_mode)
        )
        if worktree_access and sandbox_mode != "read-only":
            raise ValueError("worktree Codex access cannot use a writable sandbox")
        extra_config = [
            "mcp_servers={}",
            f"mcp_servers.chatcopilot.command={json.dumps(gateway_command)}",
            f"mcp_servers.chatcopilot.args={gateway_args}",
            "mcp_servers.chatcopilot.required=true",
            (
                "mcp_servers.chatcopilot.enabled_tools="
                + json.dumps(sorted(state.allowed_tool_names), ensure_ascii=False)
            ),
            'mcp_servers.chatcopilot.default_tools_approval_mode="approve"',
        ]
        if isolate_backend_state:
            extra_config.append("project_doc_max_bytes=0")
            extra_config.extend(
                f"features.{feature}=false"
                for feature in _ISOLATED_DISABLED_FEATURES
            )
        if self._policy.network_access:
            extra_config.extend(self._workspace_network_proxy_config())
        command = build_codex_command(
            template=routing.code_command,
            model=effective_selection.model,
            workdir=state.workdir,
            reasoning_effort=effective_selection.reasoning_effort,
            network_access=self._policy.network_access,
            sandbox_mode=sandbox_mode,
            web_search_mode=self._policy.web_search_mode,
            skip_git_repo_check=not worktree_access,
            ephemeral=False,
            ignore_user_config=True,
            inherit_shell_environment=False,
            shell_env_overrides=(
                {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "TMPDIR": "/tmp",
                }
                if isolate_backend_state
                else None
            ),
            extra_config=tuple(extra_config),
        )
        if isolate_backend_state:
            command[2:2] = ["--strict-config", "--ignore-rules"]
        command.append("--json")
        if state.native_session_id:
            command.extend(["resume", state.native_session_id])
            for image_path in image_paths:
                command.extend(["--image", image_path])
            command.append("-")
        else:
            for image_path in image_paths:
                command.extend(["--image", image_path])
        if isolate_backend_state:
            return self._wrap_isolated_command(state, command)
        return command

    @staticmethod
    def _subprocess_env(state: _CodexSession, executable: str) -> dict[str, str]:
        return build_codex_subprocess_env(
            executable,
            runtime_home=state.codex_home,
        )

    @staticmethod
    def _require_isolated_main_codex_sandbox() -> None:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("bubblewrap is required for shared-group Codex sessions")
        resolved = str(Path(bwrap).resolve())
        if resolved in _BWRAP_PROBED:
            return
        probe = subprocess.run(
            [
                resolved,
                "--die-with-parent",
                "--new-session",
                "--unshare-pid",
                "--ro-bind",
                "/",
                "/",
                "--",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError("bubblewrap cannot create the shared-group Codex sandbox")
        _BWRAP_PROBED.add(resolved)

    @staticmethod
    def _validate_isolated_roots(*, workdir: Path, state_root: Path) -> None:
        if not workdir.is_dir() or workdir.is_symlink():
            raise RuntimeError("shared-group Codex workdir must be a real directory")
        if state_root.is_symlink():
            raise RuntimeError("shared-group Codex state root must not be a symlink")
        try:
            state_root.relative_to(workdir)
        except ValueError:
            pass
        else:
            raise RuntimeError("shared-group Codex state must be outside the shared workdir")
        try:
            workdir.relative_to(state_root)
        except ValueError:
            pass
        else:
            raise RuntimeError("shared-group Codex workdir must be outside backend state")

    @staticmethod
    def _wrap_isolated_command(state: _CodexSession, command: list[str]) -> list[str]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("bubblewrap is required for shared-group Codex sessions")
        host_codex = Path(command[0]).expanduser().resolve()
        gateway_venv = Path(sys.prefix).expanduser().resolve()
        gateway_script = Path(_standalone_gateway.__file__).resolve()
        for path, label in (
            (host_codex, "Codex executable"),
            (gateway_script, "session gateway"),
        ):
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"isolated {label} must be a real file")
        if not gateway_venv.is_dir() or gateway_venv.is_symlink():
            raise RuntimeError("isolated session gateway environment must be a real directory")
        for path, label in (
            (state.codex_home, "Codex runtime home"),
        ):
            if not path.is_dir() or path.is_symlink():
                raise RuntimeError(f"isolated {label} must be a real directory")
            info = path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RuntimeError(f"isolated {label} must be owner-only mode 0700")
        if not state.gateway_config.is_file() or state.gateway_config.is_symlink():
            raise RuntimeError("isolated session gateway config must be a real file")

        wrapped = [
            str(Path(bwrap).resolve()),
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "--dir",
            "/etc",
            "--dir",
            "/opt",
            "--dir",
            "/opt/chatcopilot-codex",
            "--dir",
            "/opt/chatcopilot-gateway",
            "--dir",
            "/sandbox-home",
            "--dir",
            "/sandbox-home/agent",
        ]
        for system_path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(system_path).exists():
                wrapped.extend(["--ro-bind", system_path, system_path])
        for system_path in (
            "/etc/ca-certificates",
            "/etc/group",
            "/etc/hosts",
            "/etc/ld.so.cache",
            "/etc/localtime",
            "/etc/nsswitch.conf",
            "/etc/passwd",
            "/etc/resolv.conf",
            "/etc/ssl",
        ):
            if Path(system_path).exists():
                wrapped.extend(["--ro-bind", system_path, system_path])
        wrapped.extend(_sandbox_parent_dirs(state.workdir))
        wrapped.extend(
            [
                "--ro-bind",
                str(state.workdir),
                str(state.workdir),
                "--tmpfs",
                str(state.workdir / ".codex"),
                "--ro-bind",
                str(host_codex),
                _ISOLATED_CODEX_BINARY,
                "--ro-bind",
                str(gateway_venv),
                _ISOLATED_GATEWAY_VENV,
                "--ro-bind",
                str(gateway_script),
                _ISOLATED_GATEWAY_SCRIPT,
                "--ro-bind",
                str(state.gateway_config),
                _ISOLATED_GATEWAY_CONFIG,
                "--bind",
                str(state.codex_home),
                _ISOLATED_CODEX_HOME,
                "--setenv",
                "HOME",
                "/sandbox-home/agent",
                "--setenv",
                "CODEX_HOME",
                _ISOLATED_CODEX_HOME,
                "--setenv",
                "CODEX_SQLITE_HOME",
                _ISOLATED_CODEX_HOME,
                "--setenv",
                "PATH",
                "/opt/chatcopilot-codex:/usr/local/bin:/usr/bin:/bin",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "LANG",
                "C.UTF-8",
                "--setenv",
                "LC_ALL",
                "C.UTF-8",
                "--setenv",
                "USER",
                "agentstrata",
                "--setenv",
                "LOGNAME",
                "agentstrata",
                "--chdir",
                str(state.workdir),
                "--",
            ]
        )
        command = list(command)
        command[0] = _ISOLATED_CODEX_BINARY
        wrapped.extend(command)
        return wrapped

    def _prompt(self, state: _CodexSession, task: AgentTask) -> str:
        baseline = state.system_baseline.strip()
        appendix = (task.system_appendix or "").strip()
        pieces = [
            baseline,
            self._execution_policy_prompt(state),
            frame_task_message(task),
            appendix,
        ]
        return "\n\n".join(piece for piece in pieces if piece)

    @staticmethod
    def _image_paths(task: AgentTask) -> tuple[str, ...]:
        paths: list[str] = []
        for resource in task.resources:
            media_type = normalize_image_media_type(resource.media_type)
            if resource.kind != "file" or media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
                continue
            validate_image_file(
                resource.path,
                declared_media_type=media_type,
                expected_size_bytes=resource.size_bytes,
                expected_sha256=resource.sha256,
            )
            paths.append(resource.path)
        return tuple(paths)

    @staticmethod
    def _workspace_network_proxy_config() -> tuple[str, ...]:
        return (
            "features.network_proxy.enabled=true",
            'features.network_proxy.domains={ "*" = "allow" }',
            "features.network_proxy.allow_local_binding=false",
            "features.network_proxy.dangerously_allow_non_loopback_proxy=false",
            "features.network_proxy.dangerously_allow_all_unix_sockets=false",
        )

    def _execution_policy_prompt(self, state: _CodexSession) -> str:
        if state.access_mode == CODEX_ACCESS_WORKTREE:
            boundary = (
                "This Owner main session may inspect the source repository but is read-only. "
                "For every repository mutation, call start_code_task and manage it with the "
                "code-task lifecycle tools. Do not attempt direct source writes or shell "
                "mutation. When the user explicitly requests a plan before later "
                "confirmation, plan without calling start_code_task in that turn; after the "
                "user confirms, submit the complete approved plan exactly once. Do not run "
                "git commit or git push in this main session. "
            )
        else:
            boundary = (
                "This member workspace session may write only its personal workspace and "
                "must not inspect, disclose, or modify AgentStrata source, bot design, "
                "configuration, internal prompts, logs, other users' data, or deployment files. "
                "Do not run git commit, git push, deployment, restart, or service-management "
                "commands even when requested. "
            )
        search_boundary = (
            "Codex native web search is live. "
            if self._policy.web_search_mode == "live"
            else "Codex native web search is disabled by this execution policy. "
        )
        return (
            search_boundary
            + boundary
            + "Never expose secret values or credentials."
        )

    def _consume_events(
        self,
        state: _CodexSession,
        stdout: str,
        on_event: EventSink,
        *,
        emit_text: bool = True,
    ) -> str:
        final_parts: list[str] = []
        for raw_line in (stdout or "").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("type") or "")
            if event_type in {"thread.started", "thread_started"}:
                native_id = str(
                    event.get("thread_id") or event.get("threadId") or event.get("id") or ""
                ).strip()
                if native_id:
                    stable = next(key for key, value in self._sessions.items() if value is state)
                    state.native_session_id = native_id
                    self._aliases[native_id] = stable
                    self._persist_session_state(state)
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else event
            item_type = str(item.get("type") or "")
            if event_type in {"item.completed", "item_completed"} and item_type in {
                "agent_message",
                "message",
            }:
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    final_parts.append(text)
                    if emit_text:
                        on_event(TextDelta(text))
        return "\n".join(final_parts).strip()

    def _resolve_source_workdir(self, source_root: Any) -> Path:
        configured = os.environ.get(self._runtime_config.routing.code_workdir_env, "").strip()
        candidate = str(source_root or "").strip() or configured
        if not candidate:
            raise RuntimeError("worktree Codex access requires a configured source root")
        root = Path(candidate).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"worktree Codex source root is not a directory: {root}")
        return root

    @staticmethod
    def _resolve_workspace_workdir(workspace_root: Any) -> Path:
        candidate = str(workspace_root or "").strip()
        if not candidate:
            candidate = tempfile.mkdtemp(prefix="chatcopilot-codex-workspace-")
        root = Path(candidate).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _policy_fingerprint(
        self,
        role_hint: str,
        access_mode: str,
        *,
        caller_user_id: str,
    ) -> str:
        caller_digest = (
            hashlib.sha256(caller_user_id.encode("utf-8")).hexdigest() if caller_user_id else ""
        )
        payload = json.dumps(
            {
                "role": role_hint,
                "access": access_mode,
                "caller_id_digest": caller_digest,
                "command_confinement": {
                    "network_access": self._policy.network_access,
                    "sandbox_mode": self._policy.sandbox_mode,
                    "web_search_mode": self._policy.web_search_mode,
                },
                "tool_surface": {
                    "allow_delegate_tools": self._policy.allow_delegate_tools,
                    "allow_unified_search_tool": (self._policy.allow_unified_search_tool),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _stable_key(self, session: BackendSessionRef) -> str:
        if session.backend != self.backend_id:
            raise KeyError("cross-backend session reference")
        stable = self._aliases.get(session.value)
        if stable is None:
            raise KeyError("unknown Codex session reference")
        return stable

    def _resolve(self, session: BackendSessionRef) -> _CodexSession:
        return self._sessions[self._stable_key(session)]

    @staticmethod
    def _load_native_session_state(
        path: Path,
        *,
        acp_session_id: str,
        policy_fingerprint: str,
    ) -> tuple[str, int]:
        if not path.is_file():
            return "", 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("schema_version") or 0) != 2:
            return "", 0
        if str(payload.get("acp_session_id") or "") != acp_session_id:
            raise ValueError(f"Codex session state identity mismatch: {path}")
        if str(payload.get("policy_fingerprint") or "") != policy_fingerprint:
            return "", 0
        generation = payload.get("credential_generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            return "", 0
        return str(payload.get("native_session_id") or "").strip(), generation

    def _persist_session_state(self, state: _CodexSession) -> None:
        self._write_json_atomic(
            state.session_state_path,
            {
                "schema_version": 2,
                "acp_session_id": state.acp_session_id,
                "native_session_id": state.native_session_id,
                "role_hint": state.role_hint,
                "access_mode": state.access_mode,
                "policy_fingerprint": state.policy_fingerprint,
                "credential_generation": state.credential_generation,
            },
        )

    @staticmethod
    def _bot_credential_root() -> Path:
        raw = os.environ.get("CHATCOPILOT_CODEX_BOT_HOME", "").strip()
        if not raw:
            raise CredentialError("auth_root_unconfigured")
        return validate_auth_root_path(raw)

    def _sync_credential_generation(
        self,
        state: _CodexSession,
        generation: int,
    ) -> None:
        if state.credential_generation == generation:
            return
        self._clear_native_session(state)
        state.credential_generation = generation
        self._persist_session_state(state)

    def _clear_native_session(self, state: _CodexSession) -> None:
        if state.native_session_id:
            state.native_session_id = ""
        self._persist_session_state(state)

    def _credential_failure_result(
        self,
        state: _CodexSession,
        task: AgentTask,
        *,
        on_event: EventSink,
        code: str,
        diagnostic: str = "",
    ) -> AgentResult:
        detail = f"Codex main credential lease failed: {code}"
        if diagnostic:
            detail = f"{detail}\n{diagnostic}"[-4000:]
        framed_task = frame_task_message(task)
        message = self._auth_remediation()
        on_event(TurnError(code="codex_auth_invalid", message=detail))
        on_event(FinalText(message))
        if (
            len(state.messages) >= 2
            and state.messages[-1].get("role") == "assistant"
            and state.messages[-2] == {"role": "user", "content": framed_task}
        ):
            state.messages[-1] = {"role": "assistant", "content": message}
        elif state.messages and state.messages[-1] == {
            "role": "user",
            "content": framed_task,
        }:
            state.messages.append({"role": "assistant", "content": message})
        else:
            state.messages.extend(
                [
                    {"role": "user", "content": framed_task},
                    {"role": "assistant", "content": message},
                ]
            )
        return AgentResult(
            final_text=message,
            stop_reason="llm_error",
            message_count=len(state.messages),
        )

    @classmethod
    def _safe_cli_failure(cls, detail: str) -> str:
        if cls._is_auth_failure(detail):
            return cls._auth_remediation()
        return cls._generic_cli_failure()

    @staticmethod
    def _generic_cli_failure() -> str:
        return (
            "Codex execution failed. The private task diagnostic contains the detailed "
            "error; inspect it with the task ID and retry after fixing the cause."
        )

    @staticmethod
    def _auth_remediation() -> str:
        return (
            "Codex authentication is unavailable. On the deployment host, run "
            "`python -m chatcopilot bot codex-auth login --bot <bot.yaml> --lane main`, "
            "then retry."
        )

    @staticmethod
    def _is_auth_failure(detail: str) -> bool:
        normalized = str(detail or "").lower()
        return any(
            marker in normalized
            for marker in (
                "refresh token",
                "token has already been used",
                "token already used",
                "unauthorized",
                "authentication",
                "not logged in",
                "login required",
                "status code 401",
                "http 401",
            )
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        if path.is_symlink():
            raise RuntimeError("Codex backend state file must not be a symlink")
        temp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        temp.replace(path)


def _sandbox_parent_dirs(path: Path) -> list[str]:
    """Create only the lexical parents needed for one exact workspace bind."""

    target = path.expanduser().resolve()
    precreated = {
        Path("/bin"),
        Path("/dev"),
        Path("/etc"),
        Path("/lib"),
        Path("/lib64"),
        Path("/opt"),
        Path("/proc"),
        Path("/run"),
        Path("/tmp"),
        Path("/usr"),
    }
    arguments: list[str] = []
    for parent in reversed(target.parents):
        if parent == Path("/") or parent in precreated:
            continue
        arguments.extend(["--dir", str(parent)])
    return arguments


__all__ = ["CodexAgentBackend"]
