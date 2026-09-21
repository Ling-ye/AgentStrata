"""Codex native App Server loop with actor-bound state and host dynamic tools."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chatcopilot.agent.runtimes.codex_app_server import AppServerProjector
from chatcopilot.agent.runtimes.dynamic_tools import DynamicToolBridge
from chatcopilot.agent.context import (
    frame_task_message,
    validated_image_resource_receipts,
)
from chatcopilot.agent.context.token_estimator import estimate_prompt_tokens
from chatcopilot.agent.response_integrity import ResponseIntegrityCheck
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.turn_support import safe_emit
from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.core.scoped_process import require_bubblewrap
from chatcopilot.agent.runtimes.codex_permissions import permission_config
from chatcopilot.contracts.agent import (
    AgentResult,
    AgentTask,
    ContextSnapshotPrepared,
    EventSink,
    FinalText,
    InputResourcesDispatched,
    LlmCallStarted,
    SpanFinished,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnError,
)
from chatcopilot.contracts.runtime_adapter import (
    RuntimeCapabilities,
    RuntimeOpenRequest,
    RuntimeSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_NATIVE_RESUME,
    CAPABILITY_REPOSITORY_MUTATION,
    CAPABILITY_TOOLS,
    CodexMainSessionPolicy,
    CODEX_ACCESS_MODES,
    require_runtime_capabilities,
)
from chatcopilot.contracts.cancellation import (
    CancellationProbe,
    CancellationRequested,
)
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.core.image_content import (
    SUPPORTED_IMAGE_MEDIA_TYPES,
    normalize_image_media_type,
    validate_image_file,
)
from chatcopilot.contracts.prompt import PromptPlan
from chatcopilot.agent.context.prompt_plan import render_codex_prompt, render_codex_developer
from chatcopilot.external_tools.codex_cli.command import (
    build_app_server_command,
    build_codex_subprocess_env,
)
from chatcopilot.core.model_credentials import (
    CredentialError,
    access_credential,
    validate_auth_root_path,
)
from chatcopilot.external_tools.codex_cli.app_server import run_app_server
from chatcopilot.contracts.model_runtime import ModelSelection, ResolvedRuntimeRoute
from chatcopilot.core.codex_extensions import extension_digest, managed_extension_config
from chatcopilot.contracts.cancellation import CancellationToken, CombinedCancellation
from chatcopilot.contracts.execution import RuntimeSessionBinding, CapabilitySnapshot, HostRuntimePolicy, RuntimeFailure

if TYPE_CHECKING:
    from chatcopilot.agent.session import ToolPayloadFilter


@dataclass
class _CodexSession:
    acp_session_id: str
    prompt_plan: PromptPlan
    route: ResolvedRuntimeRoute
    allowed_tool_names: frozenset[str]
    state_root: Path
    workdir: Path
    codex_home: Path
    session_state_path: Path
    relay: DynamicToolBridge
    relay_tools: tuple[ToolDef, ...]
    relay_executor: ToolExecutor
    role_hint: str
    access_mode: str
    policy_fingerprint: str
    isolate_runtime_state: bool = False
    execution_scope: ExecutionScope | None = None
    native_session_id: str = ""
    credential_generation: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_exchanges: list[dict[str, str]] = field(default_factory=list)
    turn_lock: Any = field(default_factory=threading.Lock, repr=False)
    usage_totals: dict[str, int] | None = None
    connection: list = field(default_factory=list)
    auth: Any = field(default=None, repr=False)
    capability_snapshot: CapabilitySnapshot = field(default_factory=CapabilitySnapshot)
    host_policy: HostRuntimePolicy = field(default_factory=HostRuntimePolicy)
    restore_persisted_native_session: bool = True
    extensions: str = field(default="", repr=False)
    extension_env: dict[str, str] = field(default_factory=dict, repr=False)


_ISOLATED_CODEX_HOME = "/sandbox-home/agent/.codex"
_ISOLATED_CODEX_BINARY = "/opt/chatcopilot-codex/codex"
_BWRAP_PROBED: set[str] = set()


class CodexRuntimeAdapter:
    runtime_id = "codex"

    def __init__(
        self,
        *,
        route: ResolvedRuntimeRoute,
        tool_names: set[str],
        runtime_config: Any,
        tools: tuple[Any, ...] = (),
        tool_executor: ToolExecutor | None = None,
        tool_payload_filter: ToolPayloadFilter | None = None,
        runtime_policy: CodexMainSessionPolicy | None = None,
        turn_timeout_seconds: float | None = 21600,
        interaction_handler: Any = None,
        **_: Any,
    ) -> None:
        if route.runtime_id != "codex":
            raise ValueError("Codex adapter requires a codex runtime route")
        if runtime_config.llm.model_route() != route.model:
            raise ValueError("Codex adapter route does not match its credential configuration")
        self._route = route
        self._runtime_config = runtime_config
        self._turn_timeout = turn_timeout_seconds
        self._interaction_handler = interaction_handler
        self._tool_names = frozenset(tool_names)
        self._tools = tuple(tools)
        self._tool_executor = tool_executor
        self._tool_payload_filter = tool_payload_filter
        self._policy = runtime_policy or CodexMainSessionPolicy()
        self._capabilities = RuntimeCapabilities(
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
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def open_session(self, request: RuntimeOpenRequest) -> RuntimeSessionRef:
        if request.route != self._route:
            raise ValueError("runtime open request route does not match Codex adapter")
        require_runtime_capabilities(
            self.runtime_id, self.capabilities, request.required_capabilities
        )
        session_key = hashlib.sha256(request.session_id.encode("utf-8")).hexdigest()[:24]
        stable_id = f"acp-{session_key}"
        options = request.options
        role_hint = str(options.get("role_hint") or "user").strip().lower()
        scope = options.get("execution_scope")
        if request.host_policy.scope is not None:
            if scope is not None and scope != request.host_policy.scope:
                raise ValueError("execution scope disagrees with host policy")
            scope = request.host_policy.scope
        access_mode = "worktree" if scope is not None and scope.project_roots else "workspace"
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
        policy_fingerprint = hashlib.sha256((policy_fingerprint + request.capability_snapshot.fingerprint +
            request.route.behavior_fingerprint +
            request.host_policy.fingerprint + extension_digest(self._runtime_config.codex_extensions) + str(self._turn_timeout) +
            json.dumps(sorted(request.allowed_tool_names))).encode()).hexdigest()
        if scope is not None:
            policy_fingerprint = hashlib.sha256(
                (policy_fingerprint + replace(request.host_policy, scope=scope).fingerprint).encode()
            ).hexdigest()
        existing = self._sessions.get(stable_id)
        if existing is not None:
            if existing.policy_fingerprint == policy_fingerprint:
                return self.current_session_ref(RuntimeSessionRef(self.runtime_id, stable_id))
            self.close_session(RuntimeSessionRef(self.runtime_id, stable_id))
        if scope is not None and scope.project_roots:
            workdir = scope.project_roots[0]
        else:
            workdir = self._resolve_workspace_workdir(options.get("workspace_root"))
        state_root = (
            Path(options.get("runtime_state_root") or workdir / ".chatcopilot" / "runtime-sessions")
            .expanduser()
            .resolve()
        )
        isolate_runtime_state = bool(options.get("isolate_runtime_state"))
        if isolate_runtime_state:
            self._require_isolated_main_codex_sandbox()
        state_root.mkdir(parents=True, exist_ok=True)
        try:
            state_root.chmod(0o700)
        except OSError:
            pass
        if isolate_runtime_state:
            self._validate_isolated_roots(workdir=workdir, state_root=state_root)
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
        relay = DynamicToolBridge(
            tools=selected_tools,
            executor=executor,
            payload_filter=self._tool_payload_filter,
        )
        codex_home = state_root / f"{stable_id}.codex-home"
        session = _CodexSession(
            acp_session_id=request.session_id,
            prompt_plan=request.prompt_plan,
            route=request.route,
            allowed_tool_names=frozenset(tool.name for tool in selected_tools),
            state_root=state_root,
            workdir=workdir,
            codex_home=codex_home,
            session_state_path=session_state_path,
            relay=relay,
            relay_tools=selected_tools,
            relay_executor=executor,
            role_hint=role_hint,
            access_mode=access_mode,
            policy_fingerprint=policy_fingerprint,
            isolate_runtime_state=isolate_runtime_state,
            execution_scope=scope,
            native_session_id=native_session_id,
            credential_generation=credential_generation,
            capability_snapshot=request.capability_snapshot,
            host_policy=request.host_policy,
            restore_persisted_native_session=bool(options.get("restore_persisted_native_session", True)),
            extensions=self._runtime_config.codex_extensions if "apps" in request.host_policy.extension_grants else "",
            extension_env=dict(self._runtime_config.codex_extension_env) if "apps" in request.host_policy.extension_grants else {},
        )
        self._sessions[stable_id] = session
        self._aliases[stable_id] = stable_id
        if native_session_id:
            self._aliases[native_session_id] = stable_id
        self._persist_session_state(session)
        return self.current_session_ref(RuntimeSessionRef(self.runtime_id, stable_id))

    def stream_turn(
        self,
        session: RuntimeSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        state = self._resolve(session)
        if not state.turn_lock.acquire(blocking=False):
            raise RuntimeError("Codex thread already has an active writer")
        try:
            return self._run_owned_turn(state, task, on_event=on_event, cancellation=cancellation)
        finally:
            state.turn_lock.release()

    def _run_owned_turn(self, state, task, *, on_event, cancellation):
        buffered_delivery_events: list[Any] = []
        event_lock = threading.RLock()

        def safe_on_event(event: Any) -> None:
            with event_lock:
                if isinstance(event, TurnError):
                    from chatcopilot.core.observability_redaction import redact_observability_payload
                    secrets = (self._runtime_config.llm.api_key,
                               state.auth.access_token if state.auth is not None else "",
                               *state.extension_env.values())
                    event = replace(event, message=redact_observability_payload(event.message, secrets=secrets).value)
                safe_emit(on_event, event)

        def emit_during_turn(event: Any) -> None:
            # Final delivery remains buffered until the native turn and its
            # host-tool audit finish; observation events stream independently.
            with event_lock:
                if isinstance(event, (TextDelta, FinalText, TurnError)):
                    buffered_delivery_events.append(event)
                    return
                safe_emit(on_event, event)

        try:
            config = self._runtime_config.llm
            if config.auth_mode == "chatgpt":
                state.auth = access_credential(Path(config.credential_root), config.auth_profile)
                self._sync_credential_generation(state, state.auth.identity_epoch)
            else:
                if not config.api_key:
                    raise CredentialError("api_key_missing")
                identity_epoch = int(hashlib.sha256(config.api_key.encode()).hexdigest()[:15], 16)
                self._sync_credential_generation(state, identity_epoch)
            result = self._stream_turn(state, task, on_event=emit_during_turn, cancellation=cancellation)
        except CredentialError as exc:
            self._close_connection(state)
            self._clear_native_session(state)
            diagnostic = "\n".join(
                event.message
                for event in buffered_delivery_events
                if isinstance(event, TurnError) and event.message
            )
            return self._credential_failure_result(
                state,
                task,
                on_event=safe_on_event,
                code=exc.code,
                diagnostic=diagnostic,
            )

        for event in buffered_delivery_events:
            safe_on_event(event)
        return result

    def _stream_turn(
        self,
        state: _CodexSession,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        state.messages.append(
            {"role": "user", "content": _safe_task_ledger_message(task)}
        )
        try:
            self._ensure_clean_session_relay(state)
        except Exception as exc:  # noqa: BLE001 - return a safe runtime failure
            detail = f"Codex session relay recovery failed: {type(exc).__name__}: {exc}"
            message = self._safe_cli_failure(detail)
            on_event(TurnError(code="codex_tool_audit_failed", message=detail[-4000:]))
            on_event(FinalText(message))
            state.messages.append({"role": "assistant", "content": message})
            return AgentResult(
                final_text=message,
                stop_reason="runtime_error",
                failure=RuntimeFailure("codex_tool_audit_failed", "tool", message),
                message_count=len(state.messages),
            )
        try:
            selection = task.execution.model_selection or ModelSelection(state.route.model)
            if selection.route.auth != state.route.model.auth:
                raise ValueError("turn cannot change its authentication route")
        except (TypeError, ValueError) as exc:
            detail = f"Invalid Codex model selection: {exc}"[-4000:]
            message = (
                "The configured Codex model selection is invalid. "
                "Reset it with `/model default` or ask the operator to fix "
                "the BotSpec profile."
            )
            on_event(TurnError(code="invalid_model_selection", message=detail))
            on_event(FinalText(message))
            state.messages.append({"role": "assistant", "content": message})
            return AgentResult(
                final_text=message,
                stop_reason="runtime_error",
                failure=RuntimeFailure("invalid_model_selection", "configuration", message, True, True),
                message_count=len(state.messages),
            )
        projector: AppServerProjector | None = None
        turn_lifetime = CancellationToken()
        interaction_cancellation = CombinedCancellation(cancellation, turn_lifetime)
        trace_id = task.execution.trace.trace_id or state.acp_session_id
        llm_span_id: str | None = None
        turn_relay: DynamicToolBridge | None = None
        relay_generation: int | None = None
        successful_operations: list[str] = []
        def provider_event(event):
            if isinstance(event, SpanFinished) and event.source == "provider" and event.ok:
                if event.kind == "file_change":
                    successful_operations.append("native_write_file")
                elif event.kind == "web_search":
                    successful_operations.append("native_web_search")
            on_event(event)
        try:
            image_paths = self._image_paths(task)
            self._prepare_app_server_home(state)
            command = self._command(
                state,
                selection=selection,
                image_paths=image_paths,
            )
            subprocess_env = self._subprocess_env(state, command[0])
            prompt = self._prompt(state, task)
            developer = render_codex_developer(state.prompt_plan, execution_policy=self._execution_policy_prompt(state))
            image_receipts = validated_image_resource_receipts(task)
            tool_schemas = self._context_tool_schemas(state)
            effective_messages = ({"role": "developer", "content": developer}, {"role": "user", "content": prompt})
            captured_messages, resource_path_omission_count = (
                _replace_task_resource_paths(
                    {
                        "session": tuple(dict(message) for message in state.messages),
                        "effective": effective_messages,
                    },
                    task,
                )
            )
            prompt_estimate = estimate_prompt_tokens(effective_messages, tool_schemas)
            trace_id, parent_span_id, llm_span_id, snapshot_id = self._turn_trace_ids(
                state,
                task,
                prompt=developer + "\n" + prompt,
            )
            resumed = bool(state.native_session_id)
            context_kind = "codex_native_resume" if resumed else "codex_app_server"
            omitted: tuple[str, ...] = ("provider_internal_instructions",)
            if resumed:
                omitted += ("provider_managed_resume_context",)
            if image_receipts:
                omitted += ("binary_resource_payload_not_persisted",)
            if resource_path_omission_count:
                omitted += ("local_resource_paths",)
            on_event(
                ContextSnapshotPrepared(
                    snapshot_id=snapshot_id,
                    runtime_id=self.runtime_id,
                    model=selection.model,
                    iteration=0,
                    session_messages=tuple(captured_messages["session"]),
                    effective_messages=tuple(captured_messages["effective"]),
                    tool_schemas=tool_schemas,
                    resources=image_receipts,
                    coverage="adapter_visible",
                    omitted=omitted,
                    context_kind=context_kind,
                    trace_id=trace_id,
                    span_id=llm_span_id,
                    parent_span_id=parent_span_id,
                    depth=0,
                    estimated_tokens=int(prompt_estimate["tokens"]),
                    model_selection=selection.to_payload(),
                    resource_path_omission_count=resource_path_omission_count,
                )
            )
            on_event(
                LlmCallStarted(
                    model=selection.model,
                    iteration=0,
                    runtime_id=self.runtime_id,
                    execution_kind="runtime_execution",
                    request_parameters={"model": selection.model, "reasoning_effort": selection.reasoning_effort,
                                        "resume": resumed, "summary": "auto", "transport": "app_server_stdio"},
                    trace_id=trace_id,
                    span_id=llm_span_id,
                    parent_span_id=parent_span_id,
                    depth=0,
                    input_message_count=len(effective_messages),
                    input_estimated_tokens=int(prompt_estimate["tokens"]),
                    system_estimated_tokens=int(prompt_estimate["system_tokens"]),
                    tool_schema_count=len(tool_schemas),
                    tool_schema_estimated_tokens=int(
                        prompt_estimate["tool_schema_tokens"]
                    ),
                    estimator_version=str(prompt_estimate["estimator_version"]),
                    context_kind=context_kind,
                    context_snapshot_id=snapshot_id,
                )
            )
            turn_relay = state.relay
            relay_generation = turn_relay.begin_turn(
                trace_id=trace_id,
                parent_span_id=llm_span_id,
                depth=1,
                request_text=task.text,
            )
            projector = AppServerProjector(
                model=selection.model,
                iteration=0,
                trace_id=trace_id,
                llm_span_id=llm_span_id,
                parent_span_id=parent_span_id,
                context_snapshot_id=snapshot_id,
                on_event=provider_event,
                on_thread_started=lambda native_id: self._record_native_session_id(
                    state, native_id
                ),
                input_message_count=len(effective_messages),
                input_estimated_tokens=int(prompt_estimate["tokens"]),
                system_estimated_tokens=int(prompt_estimate["system_tokens"]),
                tool_schema_count=len(tool_schemas),
                tool_schema_estimated_tokens=int(prompt_estimate["tool_schema_tokens"]),
                estimator_version=str(prompt_estimate["estimator_version"]),
                context_kind=context_kind,
                initial_usage=state.usage_totals if resumed else {},
                on_usage=lambda usage: setattr(state, "usage_totals", dict(usage)),
            )

            def drain_live_relay_events() -> None:
                relay_error = self._emit_relay_tool_events(
                    turn_relay,
                    on_event,
                    generation=relay_generation,
                    trace_id=trace_id,
                    parent_span_id=llm_span_id,
                    require_complete=False,
                    successful_operations=successful_operations,
                )
                if relay_error:
                    raise RuntimeError(f"Codex relay audit failed: {relay_error}")

            def poll_codex_process() -> None:
                projector.flush()
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                drain_live_relay_events()
                if cancellation is not None:
                    cancellation.raise_if_cancelled()

            def host_request(method, params):
                if method != "account/chatgptAuthTokens/refresh":
                    interaction_cancellation.raise_if_cancelled()
                if method == "item/tool/call":
                    return turn_relay.call(params, main_thread_id=projector.thread_id, generation=relay_generation)
                if method == "account/chatgptAuthTokens/refresh":
                    if params.get("previousAccountId") not in {None, state.auth.account_id}:
                        raise CredentialError("account_identity_changed")
                    config = self._runtime_config.llm
                    refreshed = access_credential(Path(config.credential_root), config.auth_profile,
                        previous_token=state.auth.access_token)
                    if refreshed.identity_epoch != state.credential_generation:
                        raise CredentialError("account_identity_changed")
                    state.auth = refreshed
                    payload = refreshed.handoff()
                    payload.pop("type")
                    return payload
                if self._interaction_handler is not None:
                    return self._interaction_handler(method, params, task, interaction_cancellation)
                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                    return {"decision": "decline"}
                if method == "item/permissions/requestApproval":
                    return {"permissions": {}, "scope": "turn"}
                if method == "mcpServer/elicitation/request":
                    return {"action": "decline", "content": None}
                if method == "item/tool/requestUserInput":
                    return {"answers": {}}
                raise ValueError("unsupported App Server request")

            config = self._runtime_config.llm
            authentication = (state.auth.handoff() if config.auth_mode == "chatgpt"
                              else {"type": "apiKey", "apiKey": config.api_key})
            completed = run_app_server(
                command,
                cwd=state.workdir,
                prompt=prompt,
                timeout_seconds=self._turn_timeout,
                env=subprocess_env,
                model=selection.model,
                effort=selection.reasoning_effort,
                thread_id=state.native_session_id,
                image_paths=image_paths,
                on_notification=projector.consume_notification,
                on_thread=projector.bind_thread,
                on_poll=poll_codex_process,
                developer_instructions=developer,
                connection=state.connection, on_request=host_request,
                dynamic_tools=state.relay.schemas(), authentication=authentication,
                approval_policy="on-request" if state.host_policy.interactions_enabled else "never",
            )
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            audit_error = self._emit_relay_tool_events(
                turn_relay,
                on_event,
                generation=relay_generation,
                trace_id=trace_id,
                parent_span_id=llm_span_id,
                require_complete=True,
                successful_operations=successful_operations,
            )
            if audit_error:
                raise RuntimeError(f"Codex relay audit failed: {audit_error}")
            state.pending_exchanges.clear()
            projector.finish(returncode=completed.returncode)
        except CancellationRequested:
            self._close_connection(state)
            if projector is not None:
                projector.fail(reason="cancelled")
            self._clear_native_session(state)
            if turn_relay is not None and relay_generation is not None:
                self._emit_relay_tool_events(
                    turn_relay,
                    on_event,
                    generation=relay_generation,
                    trace_id=trace_id,
                    parent_span_id=llm_span_id,
                    require_complete=False,
                    settle_unknown=True,
                    successful_operations=successful_operations,
                )
            raise
        except Exception as exc:  # noqa: BLE001
            if projector is not None and projector.turn_id:
                self._clear_native_session(state)
            audit_error = ""
            if turn_relay is not None and relay_generation is not None:
                audit_error = self._emit_relay_tool_events(
                    turn_relay,
                    on_event,
                    generation=relay_generation,
                    trace_id=trace_id,
                    parent_span_id=llm_span_id,
                    require_complete=True,
                    successful_operations=successful_operations,
                )
            relay_reset_error = ""
            if audit_error and turn_relay is not None and relay_generation is not None:
                unknown_error = self._emit_relay_tool_events(
                    turn_relay,
                    on_event,
                    generation=relay_generation,
                    trace_id=trace_id,
                    parent_span_id=llm_span_id,
                    require_complete=False,
                    settle_unknown=True,
                    successful_operations=successful_operations,
                )
                if unknown_error:
                    audit_error = (
                        f"{audit_error}; unknown-outcome audit failed: {unknown_error}"
                    )
                try:
                    self._reset_session_relay(state)
                except Exception as reset_exc:  # noqa: BLE001 - preserve primary failure
                    relay_reset_error = (
                        f"{type(reset_exc).__name__}: {reset_exc}"
                    )
            if projector is not None:
                projector.fail(reason="codex_runtime_failed")
            self._close_connection(state)
            detail = f"Codex runtime failed: {type(exc).__name__}: {exc}"
            if audit_error:
                detail = f"{detail}; relay audit failed: {audit_error}"
            if relay_reset_error:
                detail = f"{detail}; relay reset failed: {relay_reset_error}"
            message = self._safe_cli_failure(detail)
            error_code = (
                "codex_auth_invalid" if self._is_auth_failure(detail) else "codex_runtime_failed"
            )
            on_event(TurnError(code=error_code, message=detail[-4000:]))
            on_event(FinalText(message))
            state.messages.append({"role": "assistant", "content": message})
            return AgentResult(
                final_text=message,
                stop_reason="runtime_error",
                failure=RuntimeFailure(error_code, "authentication" if error_code == "codex_auth_invalid" else "protocol", message,
                    error_code == "codex_auth_invalid", False),
                message_count=len(state.messages),
            )
        finally:
            turn_lifetime.cancel()
            if turn_relay is not None and relay_generation is not None:
                turn_relay.end_turn(relay_generation)

        if image_receipts:
            raw_turn = task.execution.turn_index
            turn_index = raw_turn if isinstance(raw_turn, int) and raw_turn >= 0 else 0
            on_event(
                InputResourcesDispatched(
                    runtime_id="codex",
                    turn_index=turn_index,
                    request_id=llm_span_id,
                    resources=image_receipts,
                )
            )

        final_text = projector.final_text
        codex_failed = completed.returncode != 0 or projector.provider_failed
        if codex_failed:
            detail = (
                projector.failure_detail or completed.stderr or final_text or "Codex App Server reported a failed turn"
            ).strip()[-4000:]
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
        integrity = ResponseIntegrityCheck().check(
            final_text,
            successful_operations=tuple(successful_operations),
        )
        if any(issue.startswith("missing_receipt:") for issue in integrity.issues):
            final_text = "未能确认该操作已完成：本轮缺少相应的可信成功回执。"
        state.messages.append({"role": "assistant", "content": final_text})
        on_event(TextDelta(final_text))
        on_event(FinalText(final_text))
        return AgentResult(
            final_text=final_text,
            stop_reason="llm_error" if codex_failed else "end_turn",
            message_count=len(state.messages),
            response_integrity=integrity,
            failure=RuntimeFailure("codex_turn_failed", "model", self._generic_cli_failure()) if codex_failed else None,
        )

    @staticmethod
    def _emit_relay_tool_events(
        relay: DynamicToolBridge,
        on_event: EventSink,
        *,
        generation: int,
        trace_id: str,
        parent_span_id: str | None,
        require_complete: bool = True,
        settle_unknown: bool = False,
        successful_operations: list[str] | None = None,
    ) -> str:
        """Project trusted in-process relay receipts onto the shared Agent event protocol."""

        try:
            events = (
                relay.drain_tool_events_with_unknown_active(generation=generation)
                if settle_unknown
                else relay.drain_tool_events(generation=generation)
                if require_complete
                else relay.drain_available_tool_events(generation=generation)
            )
        except Exception as exc:  # noqa: BLE001 - evidence failure is returned fail-closed
            return f"{type(exc).__name__}: {exc}"
        for event in events:
            if event.get("generation") != generation:
                return "relay returned evidence from a different turn generation"
            event_type = str(event.get("type") or "")
            if event_type == "agent_event":
                nested_event = event.get("event")
                if nested_event is None:
                    return "relay returned an empty nested agent event"
                if (
                    hasattr(nested_event, "trace_id")
                    and getattr(nested_event, "trace_id") != trace_id
                ):
                    return "relay returned a nested event with mismatched trace identity"
                safe_emit(on_event, nested_event)
                continue
            call_id = str(event.get("call_id") or "").strip()
            name = str(event.get("name") or "").strip()
            if not call_id or not name:
                return "relay returned an event without call identity"
            if event.get("trace_id") != trace_id:
                return "relay returned a tool event with mismatched trace identity"
            if event.get("parent_span_id") != parent_span_id:
                return "relay returned a tool event with mismatched parent span"
            event_depth = event.get("depth")
            if not isinstance(event_depth, int) or isinstance(event_depth, bool):
                return "relay returned a tool event with malformed depth"
            if event_type == "nested_event_omission":
                omitted_count = event.get("omitted_count")
                if (
                    not isinstance(omitted_count, int)
                    or isinstance(omitted_count, bool)
                    or omitted_count <= 0
                    or omitted_count > (1 << 63) - 1
                ):
                    return "relay returned a malformed nested event omission count"
                buffer_limit = event.get("buffer_limit")
                if (
                    not isinstance(buffer_limit, int)
                    or isinstance(buffer_limit, bool)
                    or buffer_limit <= 0
                ):
                    return "relay returned a malformed nested event buffer limit"
                omission_key = f"{trace_id}\0{call_id}\0nested-event-buffer-limit"
                safe_emit(
                    on_event,
                    SpanFinished(
                        name="Relay nested telemetry omitted by buffer limit",
                        kind="provider_omission",
                        ok=False,
                        summary=(
                            "Additional nested tool events exceeded the bounded relay "
                            "telemetry buffer."
                        ),
                        trace_id=trace_id,
                        span_id=(
                            "span_"
                            + hashlib.sha256(omission_key.encode("utf-8")).hexdigest()[:12]
                        ),
                        parent_span_id=call_id,
                        depth=event_depth + 1,
                        data={
                            "status": "truncated",
                            "reason": "relay_nested_event_buffer_limit",
                            "omitted_count": omitted_count,
                            "projected_event_limit": buffer_limit,
                        },
                    ),
                )
                continue
            if event_type == "tool_started":
                arguments = event.get("arguments")
                if not isinstance(arguments, dict):
                    return "relay returned malformed tool arguments"
                safe_emit(
                    on_event,
                    ToolStarted(
                        name=name,
                        arguments=dict(arguments),
                        trace_id=trace_id,
                        span_id=call_id,
                        parent_span_id=parent_span_id,
                        depth=event_depth,
                        runtime_id="codex", source="host", tool_call_id=call_id,
                        started_at=(
                            float(event["started_at"])
                            if isinstance(event.get("started_at"), (int, float))
                            and not isinstance(event.get("started_at"), bool)
                            else None
                        ),
                    )
                )
                continue
            if event_type != "tool_finished":
                return f"relay returned unknown tool event {event_type!r}"
            data = event.get("data")
            if data is not None and not isinstance(data, dict):
                return "relay returned malformed tool result data"
            ok = event.get("ok") is True
            business_data = data.get("data") if isinstance(data, dict) else None
            committed_value = (
                business_data.get("committed")
                if isinstance(business_data, dict)
                else None
            )
            has_receipt = committed_value is True or (
                ok and committed_value is not False
            )
            if has_receipt and successful_operations is not None:
                successful_operations.append(name)
            safe_emit(
                on_event,
                ToolFinished(
                    name=name,
                    ok=ok,
                    summary=str(event.get("summary") or ""),
                    error=None if ok else str(event.get("error") or "tool execution failed"),
                    trace_id=trace_id,
                    span_id=call_id,
                    parent_span_id=parent_span_id,
                    depth=event_depth,
                    runtime_id="codex", source="host", tool_call_id=call_id,
                    data=dict(data) if isinstance(data, dict) else None,
                    execution_result=event.get("execution_result"),
                    model_result={"role": "tool", "tool_call_id": call_id,
                                  "content": {"tool": name, **(dict(data) if isinstance(data, dict) else {})}},
                    finished_at=(
                        float(event["finished_at"])
                        if isinstance(event.get("finished_at"), (int, float))
                        and not isinstance(event.get("finished_at"), bool)
                        else None
                    ),
                )
            )
        return ""

    def _ensure_clean_session_relay(self, state: _CodexSession) -> None:
        try:
            stale_events = state.relay.drain_tool_events()
        except Exception:  # active or malformed previous-generation audit state
            self._reset_session_relay(state)
            return
        if stale_events:
            self._reset_session_relay(state)

    def _reset_session_relay(self, state: _CodexSession) -> None:
        state.relay.close()
        state.relay = DynamicToolBridge(tools=state.relay_tools, executor=state.relay_executor,
                                       payload_filter=self._tool_payload_filter)

    @staticmethod
    def _close_connection(state: _CodexSession) -> None:
        while state.connection:
            connection = state.connection.pop()
            try:
                try:
                    connection.interrupt()
                except (OSError, RuntimeError, ValueError):
                    pass  # A disconnected provider cannot acknowledge interrupt.
            finally:
                connection.__exit__(None, None, None)

    def close_session(self, session: RuntimeSessionRef) -> None:
        stable = self._stable_key(session)
        state = self._sessions.pop(stable, None)
        for alias, target in tuple(self._aliases.items()):
            if target == stable:
                self._aliases.pop(alias, None)
        if state is not None:
            try:
                self._close_connection(state)
                state.relay.close()
            finally:
                state.relay_executor.close()

    def cancel(self, session: RuntimeSessionRef) -> None:
        state = self._resolve(session)
        if state.connection:
            state.connection[0].interrupt()

    def is_busy(self, session: RuntimeSessionRef) -> bool:
        state = self._resolve(session)
        return bool(state.relay.active or any(connection.busy for connection in state.connection))

    def discard_session(self, session: RuntimeSessionRef) -> None:
        """Invalidate native resume before closing a consistency-poisoned session."""

        state = self._resolve(session)
        self._clear_native_session(state)
        self.close_session(session)

    def current_session_ref(self, session: RuntimeSessionRef) -> RuntimeSessionRef:
        state = self._resolve(session)
        value = state.native_session_id or self._stable_key(session)
        return RuntimeSessionRef(self.runtime_id, value)

    def set_prompt_plan(self, session: RuntimeSessionRef, plan: PromptPlan) -> None:
        self._resolve(session).prompt_plan = plan

    def record_exchange(
        self, session: RuntimeSessionRef, user_text: str, assistant_text: str
    ) -> None:
        state = self._resolve(session)
        state.messages.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
        state.pending_exchanges.extend([{"role": "user", "content": user_text},
                                        {"role": "assistant", "content": assistant_text}])

    def snapshot_messages(self, session: RuntimeSessionRef) -> list[dict[str, Any]]:
        return [dict(item) for item in self._resolve(session).messages]

    def snapshot_transcript(self, session: RuntimeSessionRef):
        from chatcopilot.contracts.execution import TranscriptSnapshot
        return TranscriptSnapshot(tuple(self.snapshot_messages(session)), "adapter_visible")

    def _command(
        self,
        state: _CodexSession,
        *,
        selection: ModelSelection | None = None,
        image_paths: tuple[str, ...] = (),
    ) -> list[str]:
        effective_selection = selection or ModelSelection(state.route.model)
        isolate_runtime_state = state.isolate_runtime_state or state.execution_scope is not None
        scope = state.execution_scope
        network_access = self._policy.network_access and state.host_policy.network_access
        extra_config = [] if state.extensions else ["mcp_servers={}"]
        if isolate_runtime_state:
            extra_config.append("project_doc_max_bytes=0")
            extra_config.extend(permission_config(
                scope, workdir=state.workdir,
                private_paths=(_ISOLATED_CODEX_HOME + "/auth.json",
                               _ISOLATED_CODEX_HOME + "/config.toml"),
                network_access=network_access,
                read_only=self._policy.sandbox_mode == "read-only",
            ))
        if not isolate_runtime_state:
            extra_config.extend(permission_config(scope, workdir=state.workdir,
                private_paths=(str(state.codex_home / "auth.json"), str(state.codex_home / "config.toml")),
                network_access=network_access, read_only=self._policy.sandbox_mode == "read-only"))
        if not self._policy.connected_apps or "apps" not in state.host_policy.extension_grants:
            extra_config.append("features.apps=false")
        if "apps" not in state.host_policy.extension_grants:
            extra_config.append("features.plugins=false")
        # Native extensions are configured by the instance operator, not installed
        # implicitly by model suggestions or a newly encountered Skill.
        extra_config.extend(["features.tool_suggest=false", "features.skill_mcp_dependency_install=false"])
        if not self._policy.image_generation or "image_generation" not in state.host_policy.native_capabilities:
            extra_config.append("features.image_generation=false")
        if "subagents" not in state.host_policy.native_capabilities:
            extra_config.extend(["features.multi_agent=false", "features.multi_agent_v2=false"])
        if "shell" not in state.host_policy.native_capabilities:
            extra_config.extend(["features.shell_tool=false", "features.unified_exec=false"])
        if state.host_policy.interactions_enabled:
            extra_config.append("features.default_mode_request_user_input=true")
        command = build_app_server_command(
            template="codex exec --model {model} --cd {workdir}",
            model=effective_selection.model,
            workdir=state.workdir,
            reasoning_effort=effective_selection.reasoning_effort,
            web_search_mode=self._policy.web_search_mode if "web_search" in state.host_policy.native_capabilities else "disabled",
            shell_env_overrides=(
                {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "TMPDIR": "/tmp",
                }
                if isolate_runtime_state
                else None
            ),
            extra_config=tuple(extra_config),
        )
        if isolate_runtime_state:
            return self._wrap_isolated_command(state, command)
        return command

    @staticmethod
    def _prepare_app_server_home(state: _CodexSession) -> None:
        state.codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.codex_home.chmod(0o700)
        # App Server has no exec --ignore-user-config flag. Only this owned home
        # supplies config; the namespace masks workspace configs and rules.
        for name in ("rules",):
            path = state.codex_home / name
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RuntimeError("Unsafe App Server runtime configuration directory")
        config = state.codex_home / "config.toml"
        if config.is_symlink():
            raise RuntimeError("Unsafe App Server runtime configuration")
        fd, temporary = tempfile.mkstemp(prefix=".config-", dir=state.codex_home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("# Managed by AgentStrata.\n" + managed_extension_config(state.extensions))
            os.replace(temporary, config)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _subprocess_env(state: _CodexSession, executable: str) -> dict[str, str]:
        return {**build_codex_subprocess_env(
            executable,
            runtime_home=state.codex_home,
        ), **state.extension_env}

    @staticmethod
    def _require_isolated_main_codex_sandbox() -> None:
        bwrap = require_bubblewrap()
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
            raise RuntimeError("shared-group Codex workdir must be outside runtime state")

    @staticmethod
    def _wrap_isolated_command(state: _CodexSession, command: list[str]) -> list[str]:
        from chatcopilot.core.scoped_process import sandbox_command
        executable = Path(command[0]).resolve(strict=True)
        scope = state.execution_scope or ExecutionScope(readable_roots=(state.workdir,))
        if not scope.native_write:
            scope = replace(scope, writable_roots=())
        inner = [_ISOLATED_CODEX_BINARY, *command[1:]]
        wrapped = sandbox_command(inner, scope=scope, cwd=state.workdir)
        if state.extension_env:
            # Parent env is already an explicit allowlist. Avoid putting secret
            # values on bwrap argv; native shell children still inherit none.
            wrapped.remove("--clearenv")
        boundary = wrapped.index("--")
        extra = ["--dir", "/opt/chatcopilot-codex", "--ro-bind", str(executable), _ISOLATED_CODEX_BINARY,
                 "--dir", "/sandbox-home/agent", "--bind", str(state.codex_home), _ISOLATED_CODEX_HOME,
                 "--tmpfs", _ISOLATED_CODEX_HOME + "/rules",
                 "--ro-bind", str(state.codex_home / "config.toml"), _ISOLATED_CODEX_HOME + "/config.toml",
                 "--setenv", "CODEX_HOME", _ISOLATED_CODEX_HOME,
                 "--setenv", "CODEX_SQLITE_HOME", _ISOLATED_CODEX_HOME]
        for root in set((*scope.readable_roots, *scope.writable_roots)):
            project_config = root / ".codex"
            if project_config.exists():
                metadata = project_config.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise RuntimeError("project config must be an owner-owned real directory")
                extra.extend(["--tmpfs", str(project_config)])
        wrapped[boundary:boundary] = extra
        return wrapped

    def _prompt(self, state: _CodexSession, task: AgentTask) -> str:
        context = task.turn_context or ""
        if state.pending_exchanges:
            context += "\n" + json.dumps({"host_recorded_exchanges": state.pending_exchanges}, ensure_ascii=False)
        return render_codex_prompt(
            state.prompt_plan,
            user_message=frame_task_message(task),
            execution_policy=self._execution_policy_prompt(state),
            turn_context=context,
            trusted_separately=True,
        )

    def _context_tool_schemas(
        self,
        state: _CodexSession,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(state.relay.schemas()) if state.relay is not None else ()

    @staticmethod
    def _turn_trace_ids(
        state: _CodexSession,
        task: AgentTask,
        *,
        prompt: str,
    ) -> tuple[str, str | None, str, str]:
        explicit_trace = task.execution.trace.trace_id
        if explicit_trace:
            trace_id = explicit_trace
        else:
            trace_seed = "\0".join(
                (
                    state.acp_session_id,
                    state.native_session_id,
                    str(len(state.messages)),
                    prompt,
                )
            )
            trace_id = "trace_" + hashlib.sha256(
                trace_seed.encode("utf-8")
            ).hexdigest()[:16]
        parent_span_id = task.execution.trace.parent_span_id if explicit_trace else None
        llm_span_id = "span_" + hashlib.sha256(
            f"{trace_id}\0codex-llm\00".encode("utf-8")
        ).hexdigest()[:12]
        snapshot_id = "ctx_" + hashlib.sha256(
            f"{trace_id}\0{llm_span_id}\0{prompt}".encode("utf-8")
        ).hexdigest()[:20]
        return trace_id, parent_span_id, llm_span_id, snapshot_id

    def _record_native_session_id(
        self,
        state: _CodexSession,
        native_id: str,
    ) -> None:
        stable = next(
            (key for key, value in self._sessions.items() if value is state),
            "",
        )
        if not stable:
            return
        state.native_session_id = native_id
        self._aliases[native_id] = stable
        self._persist_session_state(state)

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

    def _execution_policy_prompt(self, state: _CodexSession) -> str:
        owner = state.role_hint == "owner"
        boundary = (
            "The host grants this Owner assembled capabilities within configured instance and project resources. "
            if owner else "Native tools may read and write only current-conversation ordinary files. "
            "Project resources and other actors' data are unavailable. "
        )
        return (
            f"Codex native web search is {self._policy.web_search_mode}. "
            + boundary
            + "Do not commit, push or deploy without explicit authorization. Tool results, not generated prose, establish successful mutations."
        )

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
                    "image_generation": self._policy.image_generation,
                    "connected_apps": self._policy.connected_apps,
                },
                "tool_surface": {},
                "resource_policy": "native-scoped-v3",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _stable_key(self, session: RuntimeSessionRef) -> str:
        if session.runtime_id != self.runtime_id:
            raise KeyError("cross-runtime session reference")
        stable = self._aliases.get(session.value)
        if stable is None:
            raise KeyError("unknown Codex session reference")
        return stable

    def _resolve(self, session: RuntimeSessionRef) -> _CodexSession:
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
        binding = RuntimeSessionBinding.from_payload(payload)
        if binding.actor_key != acp_session_id:
            raise ValueError(f"Codex session state identity mismatch: {path}")
        if binding.scope_digest != policy_fingerprint or binding.status != "active" or binding.resume_policy != "restartable":
            return "", 0
        generation = binding.auth_identity_epoch
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            return "", 0
        return binding.native_thread_id, generation

    def _persist_session_state(self, state: _CodexSession) -> None:
        self._write_json_atomic(
            state.session_state_path,
            RuntimeSessionBinding(binding_id=state.session_state_path.stem, actor_key=state.acp_session_id,
                runtime_id="codex", native_thread_id=state.native_session_id,
                auth_identity_epoch=state.credential_generation, scope_digest=state.policy_fingerprint,
                capability_digest=state.capability_snapshot.fingerprint,
                runtime_config_digest=state.route.behavior_fingerprint,
                resume_policy="restartable" if state.restore_persisted_native_session else "live_only").to_payload(),
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
        self._close_connection(state)
        state.credential_generation = generation
        self._persist_session_state(state)

    def _clear_native_session(self, state: _CodexSession) -> None:
        state.usage_totals = None
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
        framed_task = _safe_task_ledger_message(task)
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
            stop_reason="runtime_error",
            failure=RuntimeFailure(code, "authentication", message, True, True),
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
            raise RuntimeError("Codex runtime state file must not be a symlink")
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


def _covered_by_isolated_system_mount(path: Path) -> bool:
    target = path.expanduser().resolve()
    for raw_root in ("/usr", "/bin", "/lib", "/lib64"):
        root = Path(raw_root)
        if not root.exists():
            continue
        resolved_root = root.resolve()
        if target == resolved_root or target.is_relative_to(resolved_root):
            return True
    return False


def _sandbox_parent_dirs(*paths: Path) -> list[str]:
    """Create only the lexical parents needed for exact read-only binds."""

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
    created: set[Path] = set()
    for path in paths:
        target = path.expanduser().resolve()
        for parent in reversed(target.parents):
            if parent == Path("/") or parent in precreated or parent in created:
                continue
            arguments.extend(["--dir", str(parent)])
            created.add(parent)
    return arguments


def _replace_task_resource_paths(value: Any, task: AgentTask) -> tuple[Any, int]:
    path_refs = {
        str(resource.path): f"$RESOURCE_{str(resource.sha256 or '')[:12] or index}"
        for index, resource in enumerate(task.resources)
        if str(resource.path)
    }

    def replace(item: Any) -> tuple[Any, int]:
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            count = 0
            for key, nested in item.items():
                safe_nested, nested_count = replace(nested)
                out[str(key)] = safe_nested
                count += nested_count
            return out, count
        if isinstance(item, (list, tuple)):
            out_items: list[Any] = []
            count = 0
            for nested in item:
                safe_nested, nested_count = replace(nested)
                out_items.append(safe_nested)
                count += nested_count
            return (tuple(out_items) if isinstance(item, tuple) else out_items), count
        if isinstance(item, str):
            result = item
            count = 0
            for path, reference in sorted(
                path_refs.items(), key=lambda entry: len(entry[0]), reverse=True
            ):
                occurrences = result.count(path)
                if occurrences:
                    result = result.replace(path, reference)
                    count += occurrences
            return result, count
        return item, 0

    return replace(value)


def _safe_task_ledger_message(task: AgentTask) -> str:
    safe_message, _ = _replace_task_resource_paths(frame_task_message(task), task)
    return str(safe_message)


__all__ = ["CodexRuntimeAdapter"]
