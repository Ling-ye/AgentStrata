"""Hermetic QQ message-flow scenarios over the production ACP host chain."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

from chatcopilot.agent.context.prompt_plan import PromptPlanBuilder
from chatcopilot.botspec import BotRuntimeContext
from chatcopilot.botspec.session_env import (
    build_session_env_values,
    write_private_session_env,
)
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.agent import (
    AgentResult,
    AgentTask,
    FinalText,
    ToolFinished,
    ToolStarted,
)
from chatcopilot.contracts.identity import ConversationIdentity, Identity, SessionIdentity
from chatcopilot.contracts.persona_control import PersonaDraftResult
from chatcopilot.core.allowlists import parse_numeric_allowlist
from chatcopilot.evals.capability_scenarios import (
    CapabilityScenarioContext,
    run_capability_scenario,
)
from chatcopilot.evals.models import EvalCaseDefinition, TrialObservation
from chatcopilot.middleware.acp.server import AcpChatAgent
from chatcopilot.middleware.acp.group_conversation import parse_sender_envelope
from chatcopilot.middleware.acp.transport_attestation import (
    validate_qq_group_transport_attestation,
)
from chatcopilot.middleware.acp.workspace_service import build_workspace_service
from chatcopilot.middleware.runtime.tasks import (
    EVENTS_FILENAME,
    TASK_FILENAME,
    TURN_FILENAME,
)
from chatcopilot.platforms.qq.ingress_probe import run_simulated_gateway_ingress


_SENTINEL = "QQ-FLOW-SENTINEL"
_STATE_SENTINEL = "qq-flow-state:unchanged"
_PERSONA_MARKER_PREFIX = "QQ-FLOW-PERSONA"
_REQUIRED_EVENT_KINDS = (
    "task_started",
    "middleware.identity_validated",
    "middleware.access_decision",
    "middleware.identity_activated",
    "middleware.session_materialized",
    "agent.task_submitted",
    "delivery.session_update",
    "task_finished",
)


@dataclass(frozen=True)
class _SyntheticInputs:
    member_id: str
    owner_id: str
    group_id: str
    bot_id: str
    session_key: str
    session_env_dir: Path
    workspace_root: Path
    shared_workspace: Path
    env: Mapping[str, str]


class _CapturingClient:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def session_update(self, *, session_id: str, update: Any) -> None:
        del session_id
        content = getattr(update, "content", None)
        text = getattr(content, "text", None)
        self.updates.append(str(text or ""))


class _DeterministicAgentSession:
    def __init__(
        self,
        prompt_plan: Any,
        *,
        tools: tuple[Any, ...] = (),
        workspace_service: Any = None,
        caller_role_hint: str = "user",
        permission_filter: Any = None,
    ) -> None:
        self.capabilities = SimpleNamespace(
            tool_names=frozenset(tool.name for tool in tools)
        )
        self._messages: list[dict[str, Any]] = []
        self.tasks: list[AgentTask] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.prompt_plan_set_count = 1
        self.prompt_plan = prompt_plan
        self._executor = ToolExecutor(
            tools=list(tools),
            workspace_service=workspace_service,
            caller_role_hint=caller_role_hint,
            permission_filter=permission_filter,
        )
        self._caller_role_hint = caller_role_hint

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def run_task(self, task: AgentTask, *, on_event: Any) -> AgentResult:
        self.tasks.append(task)
        self._messages.append({"role": "user", "content": task.text})
        final_text = _SENTINEL
        prefix = "/persona set group "
        if task.text.startswith(prefix) and "persona_manage" in self.capabilities.tool_names:
            arguments = {
                "operation": "set",
                "scope": "group",
                "requirement": task.text[len(prefix) :],
            }
            self.tool_calls.append({"name": "persona_manage", "arguments": arguments})
            on_event(
                ToolStarted(
                    name="persona_manage",
                    arguments=arguments,
                    span_id="synthetic-persona-manage",
                )
            )
            result = self._executor.execute(
                "persona_manage",
                arguments,
                role=self._caller_role_hint,
                request_text=task.text,
            )
            on_event(
                ToolFinished(
                    name="persona_manage",
                    ok=result.ok,
                    summary=result.summary,
                    error=result.error,
                    span_id="synthetic-persona-manage",
                    data=result.to_llm_payload(),
                )
            )
            final_text = result.summary if result.ok else (result.error or "工具执行失败")
        on_event(FinalText(text=final_text))
        self._messages.append({"role": "assistant", "content": final_text})
        return AgentResult(
            final_text=final_text,
            stop_reason="end_turn",
            message_count=self.message_count,
        )

    def set_prompt_plan(self, plan: Any) -> None:
        self.prompt_plan = plan
        self.prompt_plan_set_count += 1

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        self._messages.extend(
            (
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            )
        )

    def snapshot_messages(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._messages]


class _DeterministicAgentRuntime:
    def __init__(self, *, agent_backend: str) -> None:
        self.agent_backend = agent_backend
        self.retriever = None
        self.research_llm = SimpleNamespace(model="qq-flow-persona-draft-stub")
        self.sessions: list[_DeterministicAgentSession] = []

    def new_session(
        self,
        *,
        prompt_input: Any,
        session_providers: tuple[Any, ...] = (),
        workspace_service: Any = None,
        caller_role_hint: str = "user",
        permission_filter: Any = None,
        **_kwargs: Any,
    ) -> _DeterministicAgentSession:
        tools = tuple(
            tool
            for provider in session_providers
            for pack_tools in provider.packs.values()
            for tool in pack_tools
        )
        plan = PromptPlanBuilder().build(
            replace(prompt_input, tool_names=tuple(tool.name for tool in tools))
        )
        session = _DeterministicAgentSession(
            plan,
            tools=tools,
            workspace_service=workspace_service,
            caller_role_hint=caller_role_hint,
            permission_filter=permission_filter,
        )
        self.sessions.append(session)
        return session

    def build_unified_search_coordinator(self, **_kwargs: Any) -> None:
        return None


class _DeterministicPersonaDraftFactory:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.construction_count = 0
        self.draft_call_count = 0
        self.requests: list[dict[str, Any]] = []
        self.last_markdown = ""

    def __call__(self, **_kwargs: Any) -> _DeterministicPersonaDraftFactory:
        self.construction_count += 1
        return self

    def draft(self, **kwargs: Any) -> PersonaDraftResult:
        self.draft_call_count += 1
        self.requests.append(dict(kwargs))
        requirement = str(kwargs.get("owner_requirement") or "").strip()
        self.last_markdown = (
            "# Evaluation Persona\n\n"
            f"## Owner requirement\n{requirement}\n\n"
            f"## Deterministic marker\n{self.marker}"
        )
        return PersonaDraftResult(
            markdown=self.last_markdown,
            model="qq-flow-persona-draft-stub",
        )


def _random_numeric_id(*, excluded: set[str]) -> str:
    for _ in range(32):
        candidate = str(secrets.randbelow(8_000_000_000) + 1_000_000_000)
        if candidate not in excluded:
            return candidate
    raise RuntimeError("unable to allocate synthetic QQ identity")


def _synthetic_inputs(runtime: BotRuntimeContext, root: Path) -> _SyntheticInputs:
    selected: set[str] = set()
    member_id = _random_numeric_id(excluded=selected)
    selected.add(member_id)
    owner_id = _random_numeric_id(excluded=selected)
    selected.add(owner_id)
    group_id = _random_numeric_id(excluded=selected)
    selected.add(group_id)
    bot_id = _random_numeric_id(excluded=selected)
    session_env_dir = root / "session-env"
    workspace_root = root / "workspaces"
    shared_workspace = workspace_root / "group" / "shared"
    env = {
        "CHATCOPILOT_ADD_OWNER_IDS": owner_id,
        "CHATCOPILOT_ADD_OWNER_NAMES": "",
        "CHATCOPILOT_ADD_ADMIN_IDS": "",
        "CHATCOPILOT_ADD_ADMIN_NAMES": "",
        "CHATCOPILOT_CHAT_ID": group_id,
        "CHATCOPILOT_CHAT_KIND": "group",
        "CHATCOPILOT_EVALUATION_ENV_SNAPSHOT": "1",
        "CHATCOPILOT_GROUP_CONVERSATION_SCOPE": "chat",
        "CHATCOPILOT_SESSION_ENV_DIR": str(session_env_dir),
        "CHATCOPILOT_USER_ID": "",
        "CHATCOPILOT_USER_NAME": "",
        "CHATCOPILOT_WORKSPACE": str(shared_workspace),
        "CHATCOPILOT_WORKSPACE_ROOT": str(workspace_root),
        "CHATCOPILOT_WORKSPACE_SCOPE": "group_shared",
        "CC_SESSION_KEY": "qq:g:evaluation-shared-session",
        "QQ_ACCESS_TOKEN": secrets.token_urlsafe(32),
        "QQ_ACCOUNT": bot_id,
        "QQ_ALLOW_FROM": owner_id,
        "QQ_ALLOW_GROUPS": group_id,
        "QQ_WS_URL": "ws://127.0.0.1:1",
        "QQ_AT_PROXY_URL": "ws://127.0.0.1:1",
        "CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID": group_id,
    }
    return _SyntheticInputs(
        member_id=member_id,
        owner_id=owner_id,
        group_id=group_id,
        bot_id=bot_id,
        session_key=env["CC_SESSION_KEY"],
        session_env_dir=session_env_dir,
        workspace_root=workspace_root,
        shared_workspace=shared_workspace,
        env=env,
    )


def _frame_text(frame: Mapping[str, Any]) -> str:
    message = frame.get("message")
    if not isinstance(message, list):
        raise ValueError("synthetic OneBot message is missing segments")
    return "".join(
        str(item.get("data", {}).get("text") or "")
        for item in message
        if isinstance(item, Mapping)
        and item.get("type") == "text"
        and isinstance(item.get("data"), Mapping)
    ).strip()


def _write_message_attestation(
    inputs: _SyntheticInputs,
    *,
    sender_id: str,
    group_id: str,
    content: str,
) -> None:
    values = build_session_env_values(
        SessionIdentity(
            user_id=sender_id,
            chat_id=group_id,
            chat_kind="group",
        ),
        hook_event="message.received",
        transport_user_id=sender_id,
        hook_content=content,
    )
    write_private_session_env(
        directory=inputs.session_env_dir,
        session_key=inputs.session_key,
        values=values,
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"QQ flow artifact is not an object: {path.name}")
    return value


def _read_events(path: Path) -> tuple[Mapping[str, Any], ...]:
    events: list[Mapping[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError("QQ flow event is not an object")
        events.append(value)
    return tuple(events)


def _event_kind(event: Mapping[str, Any]) -> str:
    outer = str(event.get("event") or "")
    data = event.get("data")
    if outer == "flow_transition" and isinstance(data, Mapping):
        return str(data.get("kind") or "")
    return outer


def _is_ordered_subset(values: tuple[str, ...], required: tuple[str, ...]) -> bool:
    cursor = iter(values)
    return all(any(value == expected for value in cursor) for expected in required)


def _transition(
    events: tuple[Mapping[str, Any], ...],
    kind: str,
) -> Mapping[str, Any]:
    for event in events:
        if event.get("event") != "flow_transition":
            continue
        data = event.get("data")
        if isinstance(data, Mapping) and data.get("kind") == kind:
            return data
    return {}


def _decision(transition: Mapping[str, Any]) -> Mapping[str, Any]:
    value = transition.get("decision")
    return value if isinstance(value, Mapping) else {}


def _event_payload(
    events: tuple[Mapping[str, Any], ...],
    event_kind: str,
) -> Mapping[str, Any]:
    for event in events:
        if event.get("event") != event_kind:
            continue
        data = event.get("data")
        if isinstance(data, Mapping):
            return data
    return {}


def _task_documents(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob(TASK_FILENAME), key=lambda path: str(path)))


def _protected_persona_file_is_safe(path: Path, *, state_root: Path) -> bool:
    try:
        path.relative_to(state_root)
        file_info = path.lstat()
        if (
            stat.S_ISLNK(file_info.st_mode)
            or not stat.S_ISREG(file_info.st_mode)
            or file_info.st_nlink != 1
            or stat.S_IMODE(file_info.st_mode) != 0o600
            or (os.name == "posix" and file_info.st_uid != os.geteuid())
        ):
            return False
        current = path.parent
        while True:
            directory_info = current.lstat()
            if (
                stat.S_ISLNK(directory_info.st_mode)
                or not stat.S_ISDIR(directory_info.st_mode)
                or stat.S_IMODE(directory_info.st_mode) != 0o700
                or (os.name == "posix" and directory_info.st_uid != os.geteuid())
            ):
                return False
            if current == state_root:
                return True
            current = current.parent
    except (OSError, ValueError):
        return False


async def _run_owned_roundtrip(
    runtime: BotRuntimeContext,
    inputs: _SyntheticInputs,
) -> TrialObservation:
    continuation: dict[str, Any] = {}

    async def observe(frame: Mapping[str, Any]) -> None:
        sender_id = str(frame.get("user_id") or "")
        group_id = str(frame.get("group_id") or "")
        message_id = str(frame.get("message_id") or "")
        bot_id = str(frame.get("self_id") or "")
        content = _frame_text(frame)
        if not all((sender_id, group_id, message_id, bot_id, content)):
            raise ValueError("forwarded OneBot frame lacks synthetic turn identity")

        turn_env = dict(inputs.env)
        turn_env.update(
            {
                "CHATCOPILOT_CHAT_ID": group_id,
                "CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID": group_id,
                "QQ_ACCOUNT": bot_id,
                "QQ_ALLOW_FROM": inputs.owner_id,
                "QQ_ALLOW_GROUPS": group_id,
            }
        )

        with patch.dict(os.environ, turn_env, clear=True):
            _write_message_attestation(
                inputs,
                sender_id=sender_id,
                group_id=group_id,
                content=content,
            )
            deterministic_runtime = _DeterministicAgentRuntime(
                agent_backend=runtime.agent_backend,
            )
            client = _CapturingClient()
            host = AcpChatAgent(runtime=runtime)
            host._agent_runtime = deterministic_runtime
            host.on_connect(client)
            created = await host.new_session(cwd=str(inputs.shared_workspace))
            session_id = str(created.session_id)
            envelope = (
                f"[cc-connect sender_id={sender_id} platform=qq chat_id={group_id}]\n"
                f"{content}"
            )
            response = await host.prompt(
                [{"type": "text", "text": envelope}],
                session_id,
                message_id=message_id,
            )
            state = host._sessions[session_id]
            task_paths = _task_documents(inputs.workspace_root)
            if len(task_paths) != 1:
                raise ValueError("QQ flow must create exactly one actor-bound task")
            task_dir = task_paths[0].parent
            task_document = _read_json(task_paths[0])
            turn_document = _read_json(task_dir / TURN_FILENAME)
            events = _read_events(task_dir / EVENTS_FILENAME)
            event_kinds = tuple(_event_kind(event) for event in events)
            identity = _transition(events, "middleware.identity_validated")
            access = _transition(events, "middleware.access_decision")
            activation = _transition(events, "middleware.identity_activated")
            materialized = _transition(events, "middleware.session_materialized")
            submitted = _transition(events, "agent.task_submitted")
            delivery = _transition(events, "delivery.session_update")
            sessions = deterministic_runtime.sessions
            agent_tasks = [task for session in sessions for task in session.tasks]
            agent_task = agent_tasks[0] if len(agent_tasks) == 1 else None
            metadata = agent_task.metadata if agent_task is not None else {}
            turn_identity = state.turn_identity
            continuation.update(
                {
                    "event_kinds": list(event_kinds),
                    "required_event_order_observed": _is_ordered_subset(
                        event_kinds,
                        _REQUIRED_EVENT_KINDS,
                    ),
                    "host_session_created": bool(session_id),
                    "host_prompt_completed": str(response.stop_reason) == "end_turn",
                    "relay_allowlist_independent": not parse_numeric_allowlist(
                        turn_env["QQ_ALLOW_FROM"],
                        field="QQ_ALLOW_FROM",
                    ).allows(sender_id),
                    "attestation_identity_validated": (
                        identity.get("status") == "succeeded"
                        and _decision(identity).get("allowed") is True
                        and _decision(identity).get("code")
                        == "cc-connect-message-hook+sender-envelope"
                    ),
                    "access_allowed": (
                        access.get("status") == "succeeded"
                        and _decision(access).get("allowed") is True
                    ),
                    "actor_session_bound": (
                        turn_identity is not None
                        and state.workspace.user_id == sender_id
                        and state.workspace.chat_id == group_id
                        and state.workspace.scope == "group_shared"
                    ),
                    "role_resolved": getattr(state.role, "value", str(state.role)) == "user",
                    "identity_activation_observed": activation.get("status") == "succeeded",
                    "session_materialized": materialized.get("status") == "succeeded",
                    "task_record_started": "task_started" in event_kinds,
                    "task_record_finished": "task_finished" in event_kinds,
                    "task_status_succeeded": task_document.get("status") == "succeeded",
                    "turn_status_succeeded": turn_document.get("status") == "succeeded",
                    "turn_stop_reason_end_turn": turn_document.get("stop_reason") == "end_turn",
                    "final_text_delivered": turn_document.get("final_text_delivered") is True,
                    "prompt_plan_submitted": (
                        len(sessions) == 1 and sessions[0].prompt_plan_set_count >= 2
                    ),
                    "prompt_plan_set_count": (
                        sessions[0].prompt_plan_set_count if len(sessions) == 1 else 0
                    ),
                    "agent_task_submitted": (
                        submitted.get("status") == "succeeded"
                        and isinstance(agent_task, AgentTask)
                        and agent_task.text == content
                        and metadata.get("conversation_platform") == "qq"
                        and metadata.get("conversation_chat_kind") == "group"
                        and bool(metadata.get("turn_actor_ref"))
                    ),
                    "deterministic_agent_invocation_count": len(agent_tasks),
                    "agent_result_returned": (
                        turn_document.get("final_text") == _SENTINEL
                    ),
                    "event_translator_delivery": (
                        delivery.get("status") == "succeeded"
                        and _decision(delivery).get("code") == "session_update_emitted"
                    ),
                    "client_session_update_count": len(client.updates),
                    "client_received_sentinel": client.updates == [_SENTINEL],
                }
            )

    gateway_result = await run_simulated_gateway_ingress(
        inputs.env,
        downstream_observer=observe,
    )
    gateway = gateway_result.to_evidence()
    required_flags = (
        "required_event_order_observed",
        "host_session_created",
        "host_prompt_completed",
        "relay_allowlist_independent",
        "attestation_identity_validated",
        "access_allowed",
        "actor_session_bound",
        "role_resolved",
        "identity_activation_observed",
        "session_materialized",
        "task_record_started",
        "task_record_finished",
        "task_status_succeeded",
        "turn_status_succeeded",
        "turn_stop_reason_end_turn",
        "final_text_delivered",
        "prompt_plan_submitted",
        "agent_task_submitted",
        "agent_result_returned",
        "event_translator_delivery",
        "client_received_sentinel",
    )
    passed = gateway_result.passed and all(
        continuation.get(name) is True for name in required_flags
    ) and continuation.get("deterministic_agent_invocation_count") == 1 and continuation.get(
        "client_session_update_count"
    ) == 1
    return TrialObservation(
        final_text=_SENTINEL if passed else "",
        stop_reason="end_turn" if passed else "flow_rejected",
        post_state={
            "sentinel_before": _STATE_SENTINEL,
            "sentinel_after": _STATE_SENTINEL,
            "mutation_count": 0,
        },
        evidence=(
            {
                "kind": "qq_gateway_relay",
                **gateway,
                "passed": gateway_result.passed,
                "external_platform_write": False,
            },
            {
                "kind": "qq_owned_chain",
                **continuation,
                "passed": passed,
                "owned_chain_passed": passed,
                "gateway_relay_passed": gateway_result.passed,
                "full_external_e2e": False,
                "exercised_layers": [
                    "qq_at_relay",
                    "session_attestation_writer",
                    "acp_chat_agent",
                    "turn_orchestrator",
                    "task_observability",
                    "role_resolution",
                    "prompt_plan",
                    "agent_task_contract",
                    "event_translator",
                    "acp_client_delivery",
                ],
                "stubbed_layers": ["qq_platform", "napcat", "cc_connect", "agent_model"],
                "excluded_layers": ["external_qq_write"],
                "external_platform_write": False,
            },
        ),
    )


async def _run_attestation_mismatch(
    runtime: BotRuntimeContext,
    inputs: _SyntheticInputs,
) -> TrialObservation:
    original_text = f"original-{secrets.token_hex(12)}"
    forged_text = f"forged-{secrets.token_hex(12)}"
    message_id = f"mismatch-{secrets.token_hex(12)}"
    deterministic_runtime = _DeterministicAgentRuntime(
        agent_backend=runtime.agent_backend,
    )
    client = _CapturingClient()
    _write_message_attestation(
        inputs,
        sender_id=inputs.member_id,
        group_id=inputs.group_id,
        content=original_text,
    )
    host = AcpChatAgent(runtime=runtime)
    host._agent_runtime = deterministic_runtime
    host.on_connect(client)
    created = await host.new_session(cwd=str(inputs.shared_workspace))
    session_id = str(created.session_id)
    forged_envelope = (
        f"[cc-connect sender_id={inputs.member_id} platform=qq "
        f"chat_id={inputs.group_id}]\n{forged_text}"
    )
    response = await host.prompt(
        [{"type": "text", "text": forged_envelope}],
        session_id,
        message_id=message_id,
    )

    task_paths = _task_documents(inputs.workspace_root)
    task_path = task_paths[0] if len(task_paths) == 1 else None
    task_document = _read_json(task_path) if task_path is not None else {}
    turn_path = task_path.with_name(TURN_FILENAME) if task_path is not None else None
    turn_document = (
        _read_json(turn_path) if turn_path is not None and turn_path.is_file() else {}
    )
    events_path = task_path.with_name(EVENTS_FILENAME) if task_path is not None else None
    events = (
        _read_events(events_path)
        if events_path is not None and events_path.is_file()
        else ()
    )
    event_kinds = tuple(_event_kind(event) for event in events)
    rejection = _transition(events, "middleware.identity_rejected")
    mismatch_error_code = str(_decision(rejection).get("code") or "")
    agent_tasks = [
        task for session in deterministic_runtime.sessions for task in session.tasks
    ]

    conversation = ConversationIdentity(
        platform="qq",
        chat_kind="group",
        chat_id=inputs.group_id,
    )
    valid_envelope = parse_sender_envelope(
        (
            f"[cc-connect sender_id={inputs.member_id} platform=qq "
            f"chat_id={inputs.group_id}]\n{original_text}"
        ),
        conversation=conversation,
        message_id=f"retained-{secrets.token_hex(12)}",
    )
    retained_receipt = validate_qq_group_transport_attestation(
        valid_envelope.identity,
        valid_envelope.text,
        require_content_digest=True,
    )
    original_record_consumed = bool(
        retained_receipt is not None and retained_receipt.content_digest_matches
    )
    evidence = {
        "kind": "qq_attestation_mismatch",
        "host_session_created": bool(session_id),
        "host_prompt_completed": str(response.stop_reason) == "end_turn",
        "event_kinds": list(event_kinds),
        "identity_rejection_observed": (
            rejection.get("status") == "failed"
            and _decision(rejection).get("allowed") is False
        ),
        "mismatch_error_code": mismatch_error_code,
        "mismatch_consumed_record": not original_record_consumed,
        "original_record_consumed": original_record_consumed,
        "task_record_count": len(task_paths),
        "task_status_failed": (
            task_document.get("status") == "failed"
            and turn_document.get("status") == "failed"
            and turn_document.get("stop_reason") == mismatch_error_code
        ),
        "client_rejection_update_count": len(client.updates),
        "client_rejection_observed": (
            len(client.updates) == 1
            and _SENTINEL not in client.updates[0]
        ),
        "agent_invoked": bool(agent_tasks),
        "agent_invocation_count": len(agent_tasks),
        "agent_session_materialization_count": len(deterministic_runtime.sessions),
        "full_external_e2e": False,
        "stubbed_layers": [
            "qq_platform",
            "napcat",
            "cc_connect",
            "qq_at_relay",
            "agent_model",
        ],
        "excluded_layers": ["external_qq_write"],
        "external_platform_write": False,
    }
    passed = (
        evidence["host_session_created"] is True
        and evidence["host_prompt_completed"] is True
        and evidence["identity_rejection_observed"] is True
        and mismatch_error_code == "qq_transport_content_mismatch"
        and evidence["mismatch_consumed_record"] is False
        and original_record_consumed
        and evidence["task_record_count"] == 1
        and evidence["task_status_failed"] is True
        and evidence["client_rejection_observed"] is True
        and evidence["agent_invoked"] is False
        and evidence["agent_invocation_count"] == 0
        and evidence["agent_session_materialization_count"] == 0
    )
    evidence["passed"] = passed
    return TrialObservation(
        stop_reason="access_denied" if passed else "unexpected_allow",
        post_state={
            "sentinel_before": _STATE_SENTINEL,
            "sentinel_after": _STATE_SENTINEL,
            "mutation_count": 0,
        },
        evidence=(evidence,),
    )


async def _run_persona_roundtrip(
    runtime: BotRuntimeContext,
    inputs: _SyntheticInputs,
) -> TrialObservation:
    marker = f"{_PERSONA_MARKER_PREFIX}-{secrets.token_hex(12)}"
    persona_command = f"/persona set group 每轮保持简洁，并保留校验标记 {marker}"
    next_turn_text = "请用一句简短的话回应这条普通消息。"
    first_message_id = f"persona-{secrets.token_hex(12)}"
    second_message_id = f"ordinary-{secrets.token_hex(12)}"
    draft_factory = _DeterministicPersonaDraftFactory(marker)

    first_runtime = _DeterministicAgentRuntime(agent_backend=runtime.agent_backend)
    first_client = _CapturingClient()
    _write_message_attestation(
        inputs,
        sender_id=inputs.owner_id,
        group_id=inputs.group_id,
        content=persona_command,
    )
    first_host = AcpChatAgent(runtime=runtime)
    first_host._agent_runtime = first_runtime
    first_host.on_connect(first_client)
    first_created = await first_host.new_session(cwd=str(inputs.shared_workspace))
    first_session_id = str(first_created.session_id)
    initial_workspace = first_host._sessions[first_session_id].workspace
    if not initial_workspace.user_id:
        initial_workspace = replace(initial_workspace, user_id=inputs.owner_id)
    initial_persistent = build_workspace_service(
        initial_workspace,
        "qq",
    ).resolve_persistent_state()
    initial_snapshot = initial_persistent.persona_snapshot("group")
    first_envelope = (
        f"[cc-connect sender_id={inputs.owner_id} platform=qq "
        f"chat_id={inputs.group_id}]\n{persona_command}"
    )
    with patch(
        "chatcopilot.agent.persona.tools.PersonaDraftAgent",
        draft_factory,
    ):
        first_response = await first_host.prompt(
            [{"type": "text", "text": first_envelope}],
            first_session_id,
            message_id=first_message_id,
        )

    first_state = first_host._sessions[first_session_id]
    persistent = build_workspace_service(
        first_state.workspace,
        "qq",
    ).resolve_persistent_state()
    snapshot_after_first = persistent.persona_snapshot("group")
    persona_files = tuple(persistent.state_root.rglob("PERSONA.md"))
    protected_state_observed = (
        persistent.state_root
        == inputs.workspace_root / ".conversation-state" / "persistent"
        and len(persona_files) == 1
        and _protected_persona_file_is_safe(
            persona_files[0],
            state_root=persistent.state_root,
        )
    )

    first_task_paths = _task_documents(inputs.workspace_root)
    first_task_path = first_task_paths[0] if len(first_task_paths) == 1 else None
    first_task = _read_json(first_task_path) if first_task_path is not None else {}
    first_turn_path = (
        first_task_path.with_name(TURN_FILENAME) if first_task_path is not None else None
    )
    first_turn = (
        _read_json(first_turn_path)
        if first_turn_path is not None and first_turn_path.is_file()
        else {}
    )
    first_events_path = (
        first_task_path.with_name(EVENTS_FILENAME) if first_task_path is not None else None
    )
    first_events = (
        _read_events(first_events_path)
        if first_events_path is not None and first_events_path.is_file()
        else ()
    )
    first_event_kinds = tuple(_event_kind(event) for event in first_events)
    first_identity = _transition(first_events, "middleware.identity_validated")
    first_access = _transition(first_events, "middleware.access_decision")
    first_activation = _transition(first_events, "middleware.identity_activated")
    first_materialized = _transition(first_events, "middleware.session_materialized")
    first_submitted = _transition(first_events, "agent.task_submitted")
    first_delivery = _transition(first_events, "delivery.session_update")
    persona_tool_started = _event_payload(first_events, "tool_started")
    persona_tool_finished = _event_payload(first_events, "tool_finished")
    raw_tool_result = persona_tool_finished.get("result")
    persona_tool_result = raw_tool_result if isinstance(raw_tool_result, Mapping) else {}
    raw_persona_data = persona_tool_result.get("data")
    persona_data = raw_persona_data if isinstance(raw_persona_data, Mapping) else {}
    raw_receipt = persona_data.get("receipt")
    persona_receipt = raw_receipt if isinstance(raw_receipt, Mapping) else {}
    raw_draft = persona_data.get("draft")
    persona_draft = raw_draft if isinstance(raw_draft, Mapping) else {}
    first_main_tasks = [
        task for session in first_runtime.sessions for task in session.tasks
    ]
    first_tool_calls = [
        tool_call for session in first_runtime.sessions for tool_call in session.tool_calls
    ]
    first_session = first_runtime.sessions[0] if len(first_runtime.sessions) == 1 else None
    draft_request = draft_factory.requests[0] if len(draft_factory.requests) == 1 else {}
    mutation_hash = str(persona_receipt.get("content_sha256") or "")
    snapshot_hash = hashlib.sha256(snapshot_after_first.encode("utf-8")).hexdigest()

    second_runtime = _DeterministicAgentRuntime(agent_backend=runtime.agent_backend)
    second_client = _CapturingClient()
    _write_message_attestation(
        inputs,
        sender_id=inputs.owner_id,
        group_id=inputs.group_id,
        content=next_turn_text,
    )
    second_host = AcpChatAgent(runtime=runtime)
    second_host._agent_runtime = second_runtime
    second_host.on_connect(second_client)
    second_created = await second_host.new_session(cwd=str(inputs.shared_workspace))
    second_session_id = str(second_created.session_id)
    second_envelope = (
        f"[cc-connect sender_id={inputs.owner_id} platform=qq "
        f"chat_id={inputs.group_id}]\n{next_turn_text}"
    )
    second_response = await second_host.prompt(
        [{"type": "text", "text": second_envelope}],
        second_session_id,
        message_id=second_message_id,
    )
    second_state = second_host._sessions[second_session_id]
    second_snapshot = build_workspace_service(
        second_state.workspace,
        "qq",
    ).resolve_persistent_state().persona_snapshot("group")
    second_task_paths = _task_documents(inputs.workspace_root)
    new_task_paths = tuple(path for path in second_task_paths if path not in first_task_paths)
    second_task_path = new_task_paths[0] if len(new_task_paths) == 1 else None
    second_task = _read_json(second_task_path) if second_task_path is not None else {}
    second_turn_path = (
        second_task_path.with_name(TURN_FILENAME) if second_task_path is not None else None
    )
    second_turn = (
        _read_json(second_turn_path)
        if second_turn_path is not None and second_turn_path.is_file()
        else {}
    )
    second_events_path = (
        second_task_path.with_name(EVENTS_FILENAME) if second_task_path is not None else None
    )
    second_events = (
        _read_events(second_events_path)
        if second_events_path is not None and second_events_path.is_file()
        else ()
    )
    second_event_kinds = tuple(_event_kind(event) for event in second_events)
    second_identity = _transition(second_events, "middleware.identity_validated")
    second_access = _transition(second_events, "middleware.access_decision")
    second_activation = _transition(second_events, "middleware.identity_activated")
    second_materialized = _transition(second_events, "middleware.session_materialized")
    second_submitted = _transition(second_events, "agent.task_submitted")
    second_delivery = _transition(second_events, "delivery.session_update")
    second_sessions = second_runtime.sessions
    second_main_tasks = [
        task for session in second_sessions for task in session.tasks
    ]
    second_agent_task = second_main_tasks[0] if len(second_main_tasks) == 1 else None
    second_plan = second_sessions[0].prompt_plan if len(second_sessions) == 1 else None
    persona_layers = (
        tuple(layer for layer in second_plan.layers if layer.id == "persona.dynamic")
        if second_plan is not None
        else ()
    )

    evidence = {
        "kind": "qq_persona_flow",
        "first_turn_event_kinds": list(first_event_kinds),
        "next_turn_event_kinds": list(second_event_kinds),
        "fresh_acp_host_count": 2,
        "task_record_count": len(second_task_paths),
        "first_turn_host_session_created": bool(first_session_id),
        "first_turn_prompt_completed": str(first_response.stop_reason) == "end_turn",
        "first_turn_identity_validated": (
            first_identity.get("status") == "succeeded"
            and _decision(first_identity).get("allowed") is True
        ),
        "first_turn_access_allowed": (
            first_access.get("status") == "succeeded"
            and _decision(first_access).get("allowed") is True
        ),
        "first_turn_identity_activated": first_activation.get("status") == "succeeded",
        "first_turn_role_resolved_owner": (
            getattr(first_state.role, "value", str(first_state.role)) == "owner"
            and first_state.workspace.user_id == inputs.owner_id
            and first_state.workspace.chat_id == inputs.group_id
        ),
        "first_turn_session_materialized": (
            first_materialized.get("status") == "succeeded"
        ),
        "first_turn_agent_task_submitted": (
            first_submitted.get("status") == "succeeded"
            and len(first_main_tasks) == 1
            and first_main_tasks[0].text == persona_command
        ),
        "first_turn_persona_tool_visible": (
            first_session is not None
            and "persona_manage" in first_session.capabilities.tool_names
        ),
        "first_turn_persona_tool_called": (
            persona_tool_started.get("name") == "persona_manage"
            and len(first_tool_calls) == 1
            and first_tool_calls[0].get("name") == "persona_manage"
            and first_tool_calls[0].get("arguments", {}).get("requirement")
            == persona_command.removeprefix("/persona set group ")
        ),
        "persona_draft_stub_construct_count": draft_factory.construction_count,
        "persona_draft_stub_invocation_count": draft_factory.draft_call_count,
        "persona_draft_request_bound": (
            marker in str(draft_request.get("owner_requirement") or "")
            and draft_request.get("operation") == "set"
            and draft_request.get("current_persona") == ""
            and draft_request.get("research_required") is False
        ),
        "first_turn_persona_tool_succeeded": (
            persona_tool_finished.get("name") == "persona_manage"
            and persona_tool_finished.get("status") == "succeeded"
            and persona_tool_result.get("ok") is True
            and persona_data.get("outcome") == "saved"
        ),
        "first_turn_persona_draft_observed": (
            persona_draft.get("model") == "qq-flow-persona-draft-stub"
            and persona_draft.get("model_calls") == 0
        ),
        "first_turn_persona_receipt_committed": (
            persona_data.get("committed") is True
            and persona_receipt.get("operation") == "set"
            and persona_receipt.get("scope") == "group"
        ),
        "first_turn_task_succeeded": (
            first_task.get("status") == "succeeded"
            and first_turn.get("status") == "succeeded"
            and first_turn.get("stop_reason") == "end_turn"
        ),
        "first_turn_main_agent_invocation_count": len(first_main_tasks),
        "first_turn_client_receipt_observed": (
            len(first_client.updates) >= 2
            and mutation_hash[:16] in first_client.updates[-1]
            and _SENTINEL not in first_client.updates[-1]
            and first_delivery.get("status") == "succeeded"
        ),
        "first_turn_model_replaced": True,
        "first_turn_synthetic_tool_call": True,
        "initial_persona_hash": hashlib.sha256(initial_snapshot.encode("utf-8")).hexdigest(),
        "persisted_persona_hash": snapshot_hash,
        "mutation_receipt_hash": mutation_hash,
        "mutation_receipt_hash_matches_snapshot": (
            len(mutation_hash) == 64 and mutation_hash == snapshot_hash
        ),
        "protected_snapshot_contains_marker": marker in snapshot_after_first,
        "protected_state_observed": protected_state_observed,
        "next_turn_new_host_created": second_host is not first_host,
        "next_turn_prompt_completed": str(second_response.stop_reason) == "end_turn",
        "next_turn_identity_validated": (
            second_identity.get("status") == "succeeded"
            and _decision(second_identity).get("allowed") is True
        ),
        "next_turn_access_allowed": (
            second_access.get("status") == "succeeded"
            and _decision(second_access).get("allowed") is True
        ),
        "next_turn_identity_activated": second_activation.get("status") == "succeeded",
        "next_turn_role_resolved_owner": (
            getattr(second_state.role, "value", str(second_state.role)) == "owner"
            and second_state.workspace.user_id == inputs.owner_id
            and second_state.workspace.chat_id == inputs.group_id
        ),
        "next_turn_session_materialized": second_materialized.get("status") == "succeeded",
        "next_turn_loaded_same_snapshot": (
            bool(snapshot_after_first) and second_snapshot == snapshot_after_first
        ),
        "next_turn_prompt_persona_layer_count": len(persona_layers),
        "next_turn_prompt_contains_marker": (
            len(persona_layers) == 1 and marker in persona_layers[0].content
        ),
        "next_turn_agent_task_submitted": (
            second_submitted.get("status") == "succeeded"
            and isinstance(second_agent_task, AgentTask)
            and second_agent_task.text == next_turn_text
        ),
        "next_turn_main_agent_invocation_count": len(second_main_tasks),
        "next_turn_event_translator_delivery": (
            second_delivery.get("status") == "succeeded"
            and _decision(second_delivery).get("code") == "session_update_emitted"
        ),
        "next_turn_client_session_update_count": len(second_client.updates),
        "next_turn_client_received_sentinel": second_client.updates == [_SENTINEL],
        "next_turn_task_succeeded": (
            second_task.get("status") == "succeeded"
            and second_turn.get("status") == "succeeded"
            and second_turn.get("stop_reason") == "end_turn"
            and second_turn.get("final_text") == _SENTINEL
        ),
        "full_external_e2e": False,
        "exercised_layers": [
            "session_attestation_writer",
            "sender_envelope",
            "transport_attestation_consumer",
            "acp_admission",
            "role_resolution",
            "persona_manage_tool",
            "persona_persistent_state",
            "task_observability",
            "acp_chat_agent",
            "prompt_plan",
            "agent_task_contract",
            "event_translator",
            "acp_client_delivery",
        ],
        "stubbed_layers": [
            "qq_platform",
            "napcat",
            "cc_connect",
            "qq_at_relay",
            "persona_draft_agent",
            "agent_model",
        ],
        "excluded_layers": ["external_qq_write"],
        "external_platform_write": False,
    }
    required_true = (
        "first_turn_host_session_created",
        "first_turn_prompt_completed",
        "first_turn_identity_validated",
        "first_turn_access_allowed",
        "first_turn_identity_activated",
        "first_turn_role_resolved_owner",
        "first_turn_session_materialized",
        "first_turn_agent_task_submitted",
        "first_turn_persona_tool_visible",
        "first_turn_persona_tool_called",
        "persona_draft_request_bound",
        "first_turn_persona_tool_succeeded",
        "first_turn_persona_draft_observed",
        "first_turn_persona_receipt_committed",
        "first_turn_task_succeeded",
        "first_turn_client_receipt_observed",
        "first_turn_model_replaced",
        "first_turn_synthetic_tool_call",
        "mutation_receipt_hash_matches_snapshot",
        "protected_snapshot_contains_marker",
        "protected_state_observed",
        "next_turn_new_host_created",
        "next_turn_prompt_completed",
        "next_turn_identity_validated",
        "next_turn_access_allowed",
        "next_turn_identity_activated",
        "next_turn_role_resolved_owner",
        "next_turn_session_materialized",
        "next_turn_loaded_same_snapshot",
        "next_turn_prompt_contains_marker",
        "next_turn_agent_task_submitted",
        "next_turn_event_translator_delivery",
        "next_turn_client_received_sentinel",
        "next_turn_task_succeeded",
    )
    passed = (
        all(evidence.get(name) is True for name in required_true)
        and evidence["fresh_acp_host_count"] == 2
        and evidence["task_record_count"] == 2
        and evidence["persona_draft_stub_construct_count"] == 1
        and evidence["persona_draft_stub_invocation_count"] == 1
        and evidence["first_turn_main_agent_invocation_count"] == 1
        and evidence["next_turn_prompt_persona_layer_count"] == 1
        and evidence["next_turn_main_agent_invocation_count"] == 1
        and evidence["next_turn_client_session_update_count"] == 1
    )
    evidence["passed"] = passed
    return TrialObservation(
        final_text=_SENTINEL if passed else "",
        stop_reason="end_turn" if passed else "flow_rejected",
        post_state={
            "sentinel_before": _STATE_SENTINEL,
            "sentinel_after": _STATE_SENTINEL,
            "mutation_count": 0,
        },
        evidence=(evidence,),
    )


def run_qq_flow_scenario(
    case: EvalCaseDefinition,
    *,
    runtime: BotRuntimeContext,
    workspace_root: Path,
) -> TrialObservation:
    """Run one QQ Case with synthetic identities and no inherited machine config."""

    if runtime.platform_type != "qq":
        raise ValueError("QQ message-flow evaluation requires a QQ Bot")
    root = Path(workspace_root).resolve()
    with tempfile.TemporaryDirectory(prefix=".qq-flow-", dir=root) as raw_root:
        private_root = Path(raw_root)
        private_root.chmod(0o700)
        inputs = _synthetic_inputs(runtime, private_root)
        with patch.dict(os.environ, dict(inputs.env), clear=True):
            if case.case_id == "qq-synthetic-roundtrip":
                return asyncio.run(_run_owned_roundtrip(runtime, inputs))
            if case.case_id == "qq-attestation-mismatch-denied":
                return asyncio.run(_run_attestation_mismatch(runtime, inputs))
            if case.case_id == "qq-persona-persistence-next-turn":
                return asyncio.run(_run_persona_roundtrip(runtime, inputs))
            context = CapabilityScenarioContext(
                platform_type=runtime.platform_type,
                env=dict(inputs.env),
                owners=(Identity(user_id=inputs.owner_id),),
                admins=(),
                prompt_profile=runtime.prompt_profile,
                member_id=inputs.member_id,
                group_id=inputs.group_id,
            )
            return run_capability_scenario(case, context=context)


__all__ = ["run_qq_flow_scenario"]
