"""Codex CLI main-agent backend with native resume and a scoped MCP gateway."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatcopilot.agent.context import frame_task_message
from chatcopilot.contracts.agent import (
    AgentResult,
    AgentTask,
    EventSink,
    FinalText,
    TextDelta,
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
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.backends.session_relay import SessionToolRelay


@dataclass
class _CodexSession:
    acp_session_id: str
    system_baseline: str
    allowed_tool_names: frozenset[str]
    gateway_config: Path
    workdir: Path
    codex_home: Path
    session_state_path: Path
    relay: SessionToolRelay
    role_hint: str
    access_mode: str
    policy_fingerprint: str
    native_session_id: str = ""
    credential_generation: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


class CodexAgentBackend:
    backend_id = "codex"

    def __init__(
        self,
        *,
        tool_names: set[str],
        runtime_config: Any,
        tools: tuple[Any, ...] = (),
        tool_executor: ToolExecutor | None = None,
        backend_policy: CodexMainSessionPolicy | None = None,
        **_: Any,
    ) -> None:
        self._runtime_config = runtime_config
        self._tool_names = frozenset(tool_names)
        self._tools = tuple(tools)
        self._tool_executor = tool_executor
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
        state_root = Path(
            options.get("backend_state_root")
            or workdir / ".chatcopilot" / "backend-sessions"
        ).expanduser().resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        try:
            state_root.chmod(0o700)
        except OSError:
            pass
        gateway_config = state_root / f"{stable_id}.gateway.json"
        audit_path = state_root / f"{stable_id}.audit.jsonl"
        session_state_path = state_root / f"{stable_id}.session.json"
        native_session_id, credential_generation = self._load_native_session_state(
            session_state_path,
            acp_session_id=request.session_id,
            policy_fingerprint=policy_fingerprint,
        )
        allowed_tool_names = request.allowed_tool_names & self._tool_names
        selected_tools = tuple(
            tool for tool in self._tools if tool.name in allowed_tool_names
        )
        executor = self._tool_executor or ToolExecutor(
            tools=list(selected_tools),
            caller_role_hint=role_hint,
        )
        relay = SessionToolRelay(tools=selected_tools, executor=executor)
        relay_endpoint = relay.start()
        payload = {
            "schema_version": 1,
            "session_id": request.session_id,
            "allowed_tools": sorted(tool.name for tool in selected_tools),
            "role_hint": role_hint,
            "access_mode": access_mode,
            "policy_fingerprint": policy_fingerprint,
            "audit_path": str(audit_path),
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
            workdir=workdir,
            codex_home=codex_home,
            session_state_path=session_state_path,
            relay=relay,
            role_hint=role_hint,
            access_mode=access_mode,
            policy_fingerprint=policy_fingerprint,
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
            command = self._command(
                state,
                selection=selection,
                image_paths=self._image_paths(task),
            )
            completed = run_codex_process(
                command,
                cwd=state.workdir,
                prompt=self._prompt(state, task),
                timeout_seconds=self._runtime_config.routing.code_timeout_seconds,
                env=self._subprocess_env(state, command[0]),
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"Codex backend failed: {type(exc).__name__}: {exc}"
            message = self._safe_cli_failure(detail)
            error_code = (
                "codex_auth_invalid"
                if self._is_auth_failure(detail)
                else "codex_backend_failed"
            )
            on_event(TurnError(code=error_code, message=detail[-4000:]))
            on_event(FinalText(message))
            state.messages.append({"role": "assistant", "content": message})
            return AgentResult(
                final_text=message,
                stop_reason="llm_error",
                message_count=len(state.messages),
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

    def close_session(self, session: BackendSessionRef) -> None:
        stable = self._stable_key(session)
        state = self._sessions.pop(stable, None)
        for alias, target in tuple(self._aliases.items()):
            if target == stable:
                self._aliases.pop(alias, None)
        if state is not None:
            state.relay.close()
            state.gateway_config.unlink(missing_ok=True)

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
        gateway_args = json.dumps(
            [
                "-m",
                "chatcopilot",
                "mcp-session-gateway",
                str(state.gateway_config),
            ],
            ensure_ascii=False,
        )
        worktree_access = state.access_mode == CODEX_ACCESS_WORKTREE
        extra_config = [
            "mcp_servers={}",
            f'mcp_servers.chatcopilot.command={json.dumps(sys.executable)}',
            f"mcp_servers.chatcopilot.args={gateway_args}",
            *self._workspace_network_proxy_config(),
        ]
        command = build_codex_command(
            template=routing.code_command,
            model=effective_selection.model,
            workdir=state.workdir,
            reasoning_effort=effective_selection.reasoning_effort,
            network_access=True,
            sandbox_mode="read-only" if worktree_access else "workspace-write",
            web_search_mode="live",
            ephemeral=False,
            ignore_user_config=True,
            inherit_shell_environment=False,
            extra_config=tuple(extra_config),
        )
        command.append("--json")
        if state.native_session_id:
            command.extend(["resume", state.native_session_id])
            for image_path in image_paths:
                command.extend(["--image", image_path])
            command.append("-")
        else:
            for image_path in image_paths:
                command.extend(["--image", image_path])
        return command

    @staticmethod
    def _subprocess_env(state: _CodexSession, executable: str) -> dict[str, str]:
        return build_codex_subprocess_env(
            executable,
            runtime_home=state.codex_home,
        )

    def _prompt(self, state: _CodexSession, task: AgentTask) -> str:
        baseline = state.system_baseline.strip()
        appendix = (task.system_appendix or "").strip()
        pieces = [
            baseline,
            self._execution_policy_prompt(state),
            appendix,
            frame_task_message(task),
        ]
        return "\n\n".join(piece for piece in pieces if piece)

    @staticmethod
    def _image_paths(task: AgentTask) -> tuple[str, ...]:
        paths: list[str] = []
        for resource in task.resources:
            media_type = normalize_image_media_type(resource.media_type)
            if (
                resource.kind != "file"
                or media_type not in SUPPORTED_IMAGE_MEDIA_TYPES
            ):
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

    @staticmethod
    def _execution_policy_prompt(state: _CodexSession) -> str:
        if state.access_mode == CODEX_ACCESS_WORKTREE:
            boundary = (
                "This Owner main session may inspect the source repository but is read-only. "
                "For every repository mutation, call start_code_task and manage it with the "
                "code-task lifecycle tools. Do not attempt direct source writes or shell "
                "mutation."
            )
        else:
            boundary = (
                "This member workspace session may write only its personal workspace and "
                "must not modify AgentStrata source, bot design, or deployment files."
            )
        return (
            "Codex native web search is live. "
            + boundary
            + " Git commit and git push are allowed only when the current user request "
            "explicitly asks for them. Never expose secret values or credentials."
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
                    event.get("thread_id")
                    or event.get("threadId")
                    or event.get("id")
                    or ""
                ).strip()
                if native_id:
                    stable = next(
                        key for key, value in self._sessions.items() if value is state
                    )
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
        configured = os.environ.get(
            self._runtime_config.routing.code_workdir_env, ""
        ).strip()
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
            hashlib.sha256(caller_user_id.encode("utf-8")).hexdigest()
            if caller_user_id
            else ""
        )
        payload = json.dumps(
            {
                "role": role_hint,
                "access": access_mode,
                "caller_id_digest": caller_digest,
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
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
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
            and state.messages[-2]
            == {"role": "user", "content": framed_task}
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


__all__ = ["CodexAgentBackend"]
