"""Thin ACP server that projects ACP calls onto authenticated Gateway RPC."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from typing import Any, TypeAlias
from uuid import uuid4

from acp import (
    Agent,
    AuthenticateResponse,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PROTOCOL_VERSION,
    PromptResponse,
    SetSessionModeResponse,
    run_agent,
    update_agent_message_text,
)
from acp.exceptions import RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AgentAuthCapabilities,
    AcpMcpServer,
    AudioContentBlock,
    AuthMethodAgent,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    ListSessionsResponse,
    McpCapabilities,
    McpServerStdio,
    PromptCapabilities,
    ResourceContentBlock,
    ResumeSessionResponse,
    SetSessionConfigOptionResponse,
    SessionCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SseMcpServer,
    TextContentBlock,
)

from chatcopilot.contracts.gateway_rpc import (
    ChatAbortParams,
    ChatAbortResult,
    ChatErrorEvent,
    ChatFinalEvent,
    ChatSendParams,
    ChatSendResult,
    ChatUpdateEvent,
    GatewayMethodResult,
    GatewayRequestParams,
    RunSnapshot,
    RunsGetParams,
    RunsGetResult,
    RunsLatestParams,
    RunsLatestResult,
    SessionSnapshot,
    SessionsCreateParams,
    SessionsCreateResult,
    SessionsGetParams,
    SessionsGetResult,
    SessionsListParams,
    SessionsListResult,
    SessionsPatchParams,
    SessionsPatchResult,
    TextRpcSegment,
)
from chatcopilot.gateway.rpc_validation import MAX_RPC_TEXT_CHARS
from chatcopilot.protocols.gateway_client import (
    GatewayClientConfig,
    GatewayClientError,
    GatewayConnectionClosed,
    GatewayEventSubscriptionProtocol,
    GatewayMutationOutcomeUnknown,
    GatewayRecoveryRequired,
    GatewayRemoteError,
    GatewayRpcClientProtocol,
    GatewayWebSocketClient,
    TypedGatewayEvent,
)


LOCAL_AUTH_METHOD_ID = "gateway-local"
_ACP_GATEWAY_SCOPES = ("gateway.read", "chat.write", "chat.abort")
_CHAT_EVENTS = frozenset({"chat.update", "chat.final", "chat.error"})
_AcpMcpServer: TypeAlias = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio
_AcpContentBlock: TypeAlias = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


@dataclass(frozen=True)
class GatewayAcpRuntimeConfig:
    gateway: GatewayClientConfig
    local_auth_method_id: str = LOCAL_AUTH_METHOD_ID
    implementation_name: str = "agentstrata-gateway-acp"
    implementation_title: str = "AgentStrata Gateway"
    implementation_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.gateway.scopes != _ACP_GATEWAY_SCOPES:
            raise ValueError("ACP Gateway credentials require exact read, write, and abort scopes")
        for value, label in (
            (self.local_auth_method_id, "local_auth_method_id"),
            (self.implementation_name, "implementation_name"),
            (self.implementation_title, "implementation_title"),
            (self.implementation_version, "implementation_version"),
        ):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError(f"{label} is invalid")


@dataclass
class _ActiveTurn:
    session_id: str
    send_params: ChatSendParams | None
    send_idempotency_key: str | None
    abort_idempotency_key: str = field(default_factory=lambda: f"acp-abort-{uuid4().hex}")
    run_id: str | None = None
    cancel_requested: bool = False
    abort_requested: bool = False
    recovery_required: bool = False
    terminal: bool = False
    recovered_from_gateway: bool = False
    abort_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class GatewayAcpAgent(Agent):
    """Text-only ACP facade; identity and execution remain owned by the Gateway."""

    _conn: Client

    def __init__(
        self,
        gateway: GatewayRpcClientProtocol,
        *,
        local_auth_method_id: str = LOCAL_AUTH_METHOD_ID,
        implementation_name: str = "agentstrata-gateway-acp",
        implementation_title: str = "AgentStrata Gateway",
        implementation_version: str = "1.0.0",
    ) -> None:
        if not gateway.connected:
            raise ValueError("Gateway client must finish authentication before ACP starts")
        self._gateway = gateway
        self._local_auth_method_id = local_auth_method_id
        self._implementation_name = implementation_name
        self._implementation_title = implementation_title
        self._implementation_version = implementation_version
        self._authenticated = False
        self._known_sessions: set[str] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._recovery_checked_sessions: set[str] = set()

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del protocol_version, client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False,
                    audio=False,
                    embedded_context=False,
                ),
                mcp_capabilities=McpCapabilities(http=False, sse=False, acp=False),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                ),
                auth=AgentAuthCapabilities(),
            ),
            auth_methods=[
                AuthMethodAgent(
                    id=self._local_auth_method_id,
                    name="Authenticated local Gateway",
                    description="Uses the ACP process credential already bound to this Gateway.",
                )
            ],
            agent_info=Implementation(
                name=self._implementation_name,
                title=self._implementation_title,
                version=self._implementation_version,
            ),
        )

    async def authenticate(
        self,
        method_id: str,
        **kwargs: Any,
    ) -> AuthenticateResponse:
        del kwargs
        if method_id != self._local_auth_method_id or not self._gateway.connected:
            raise RequestError.auth_required({"code": "local_gateway_auth_required"})
        self._authenticated = True
        return AuthenticateResponse()

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[_AcpMcpServer] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del cwd, kwargs
        self._require_authenticated()
        _reject_external_runtime_context(additional_directories, mcp_servers)
        result = await self._request(
            "sessions.create",
            SessionsCreateParams(),
            idempotency_key=f"acp-session-{uuid4().hex}",
        )
        if not isinstance(result, SessionsCreateResult):
            raise _gateway_request_error("invalid_session_result")
        self._known_sessions.add(result.session.session_id)
        self._recovery_checked_sessions.add(result.session.session_id)
        return NewSessionResponse(
            session_id=result.session.session_id,
            modes=_session_modes(result.session),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[_AcpMcpServer] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        del cwd, kwargs
        self._require_authenticated()
        _reject_external_runtime_context(additional_directories, mcp_servers)
        result = await self._request("sessions.get", SessionsGetParams(session_id=session_id))
        if not isinstance(result, SessionsGetResult) or result.session.session_id != session_id:
            raise _gateway_request_error("invalid_session_result")
        self._known_sessions.add(session_id)
        await self._remember_active_run(result.session)
        return LoadSessionResponse(modes=_session_modes(result.session))

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        del cwd, kwargs
        self._require_authenticated()
        gateway_cursor = _parse_acp_cursor(cursor)
        result = await self._request(
            "sessions.list",
            SessionsListParams(cursor=gateway_cursor, limit=50),
        )
        if not isinstance(result, SessionsListResult):
            raise _gateway_request_error("invalid_session_result")
        self._known_sessions.update(item.session_id for item in result.sessions)
        return ListSessionsResponse(
            sessions=[
                SessionInfo(
                    session_id=item.session_id,
                    cwd="",
                    title=f"Gateway session {item.session_id[:8]}",
                )
                for item in result.sessions
            ],
            next_cursor=(str(result.next_cursor) if result.next_cursor is not None else None),
        )

    async def set_session_mode(
        self,
        session_id: str,
        mode_id: str,
        **kwargs: Any,
    ) -> SetSessionModeResponse:
        del kwargs
        self._require_authenticated()
        result = await self._request(
            "sessions.patch",
            SessionsPatchParams(session_id=session_id, mode=mode_id),
            idempotency_key=f"acp-mode-{uuid4().hex}",
        )
        if not isinstance(result, SessionsPatchResult) or result.session.session_id != session_id:
            raise _gateway_request_error("invalid_session_result")
        self._known_sessions.add(session_id)
        return SetSessionModeResponse()

    async def prompt(
        self,
        session_id: str,
        prompt: list[_AcpContentBlock],
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        self._require_local_authentication()
        text = _text_prompt(prompt)
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            pending = self._active_turns.get(session_id)
            if pending is None and session_id not in self._recovery_checked_sessions:
                pending = await self._discover_durable_turn(session_id)
            if pending is not None:
                if not pending.recovery_required:
                    raise _gateway_request_error("session_run_already_active")
                state = await self._reconcile_pending_turn(pending)
                if state == "active":
                    raise RequestError(
                        -32005,
                        "Previous Gateway turn is still active",
                        {"code": "previous_turn_still_active"},
                    )
                raise RequestError(
                    -32005,
                    "Previous Gateway turn was recovered; retry this prompt",
                    {"code": "previous_turn_recovered_retry_prompt"},
                )
            if not self._gateway.connected:
                raise _gateway_request_error("gateway_not_connected")
            return await self._run_prompt(session_id, text)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._require_local_authentication()
        turn = self._active_turns.get(session_id)
        if turn is None or turn.terminal:
            return
        turn.cancel_requested = True
        if turn.run_id is None:
            return
        try:
            await self._abort_turn(turn)
        except GatewayClientError as exc:
            raise _gateway_request_error(exc.code) from exc

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse | None:
        del config_id, session_id, value, kwargs
        raise RequestError.method_not_found("session/set_config_option")

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[_AcpMcpServer] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        del session_id, cwd, additional_directories, mcp_servers, kwargs
        raise RequestError.method_not_found("session/fork")

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[_AcpMcpServer] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        del session_id, cwd, additional_directories, mcp_servers, kwargs
        raise RequestError.method_not_found("session/resume")

    async def close_session(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> CloseSessionResponse | None:
        del session_id, kwargs
        raise RequestError.method_not_found("session/close")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del params
        raise RequestError.method_not_found(method)

    async def _run_prompt(self, session_id: str, text: str) -> PromptResponse:
        if session_id in self._active_turns:
            raise _gateway_request_error("session_run_already_active")
        subscription = self._gateway.subscribe(
            events=_CHAT_EVENTS,
            session_id=session_id,
        )
        send_params = ChatSendParams(
            session_id=session_id,
            segments=(TextRpcSegment(text),),
        )
        turn = _ActiveTurn(
            session_id=session_id,
            send_params=send_params,
            send_idempotency_key=f"acp-chat-{uuid4().hex}",
        )
        self._active_turns[session_id] = turn
        retain_for_recovery = False
        try:
            result = await self._submit_chat_turn(turn)
            if not isinstance(result, ChatSendResult) or result.session_id != session_id:
                raise _gateway_request_error("invalid_chat_result")
            turn.run_id = result.run_id
            self._known_sessions.add(session_id)
            if turn.cancel_requested:
                try:
                    await self._abort_turn(turn)
                except GatewayClientError:
                    pass
            snapshot = await self._get_run(turn)
            terminal = await self._terminal_response(turn, snapshot)
            if terminal is not None:
                return terminal
            return await self._consume_turn_events(turn, subscription)
        except GatewayRecoveryRequired as exc:
            turn.recovery_required = True
            retain_for_recovery = True
            raise RequestError(
                -32004,
                "Gateway event recovery required",
                {"code": exc.code},
            ) from exc
        except (GatewayMutationOutcomeUnknown, GatewayConnectionClosed) as exc:
            turn.recovery_required = True
            retain_for_recovery = True
            raise _gateway_request_error(exc.code) from exc
        finally:
            subscription.close()
            if not retain_for_recovery:
                turn.terminal = True
            if not retain_for_recovery and self._active_turns.get(session_id) is turn:
                self._active_turns.pop(session_id, None)

    async def _submit_chat_turn(self, turn: _ActiveTurn) -> ChatSendResult:
        if turn.send_params is None or turn.send_idempotency_key is None:
            raise _gateway_request_error("missing_mutation_recovery_identity")
        try:
            result = await self._gateway.request(
                "chat.send",
                turn.send_params,
                idempotency_key=turn.send_idempotency_key,
            )
        except GatewayMutationOutcomeUnknown:
            raise
        except GatewayRemoteError as exc:
            raise RequestError(
                -32002,
                "Gateway request was rejected",
                {"code": exc.code},
            ) from exc
        except GatewayClientError:
            raise
        if not isinstance(result, ChatSendResult):
            raise _gateway_request_error("invalid_chat_result")
        return result

    async def _get_run(self, turn: _ActiveTurn) -> RunSnapshot:
        if turn.run_id is None:
            raise _gateway_request_error("invalid_chat_result")
        try:
            result = await self._gateway.request(
                "runs.get",
                RunsGetParams(session_id=turn.session_id, run_id=turn.run_id),
            )
        except GatewayRemoteError as exc:
            raise RequestError(
                -32002,
                "Gateway request was rejected",
                {"code": exc.code},
            ) from exc
        if not isinstance(result, RunsGetResult):
            raise _gateway_request_error("invalid_run_result")
        snapshot = result.run
        if snapshot.session_id != turn.session_id or snapshot.run_id != turn.run_id:
            raise _gateway_request_error("invalid_run_result")
        return snapshot

    async def _terminal_response(
        self,
        turn: _ActiveTurn,
        snapshot: RunSnapshot,
    ) -> PromptResponse | None:
        if snapshot.state in {"accepted", "running", "abort_requested"}:
            return None
        if snapshot.state == "recovery_required":
            raise GatewayRecoveryRequired(
                "run_recovery_required",
                "Gateway run requires recovery",
            )
        turn.terminal = True
        if snapshot.state == "failed":
            raise RequestError(
                -32003,
                "Gateway chat failed",
                {"code": snapshot.error_code or "run_failed", "retryable": False},
            )
        final_text = _snapshot_text(snapshot)
        if final_text:
            await self._send_text_update(turn.session_id, final_text)
        return PromptResponse(
            stop_reason="cancelled" if snapshot.state == "aborted" else "end_turn"
        )

    async def _reconcile_pending_turn(self, turn: _ActiveTurn) -> str:
        try:
            await self._gateway.ensure_connected()
            if not turn.recovered_from_gateway:
                result = await self._submit_chat_turn(turn)
                if result.session_id != turn.session_id:
                    raise _gateway_request_error("invalid_chat_result")
                if turn.run_id is not None and turn.run_id != result.run_id:
                    raise _gateway_request_error("mutation_reconciliation_conflict")
                turn.run_id = result.run_id
            if turn.cancel_requested:
                await self._abort_turn(turn)
            snapshot = await self._get_run(turn)
            terminal = await self._terminal_response(turn, snapshot)
        except (
            GatewayMutationOutcomeUnknown,
            GatewayConnectionClosed,
            GatewayRecoveryRequired,
        ) as exc:
            turn.recovery_required = True
            raise _gateway_request_error(exc.code) from exc
        except RequestError:
            if turn.terminal:
                turn.recovery_required = False
                self._recovery_checked_sessions.add(turn.session_id)
                if self._active_turns.get(turn.session_id) is turn:
                    self._active_turns.pop(turn.session_id, None)
            raise
        if terminal is None:
            turn.recovery_required = True
            return "active"
        turn.recovery_required = False
        self._recovery_checked_sessions.add(turn.session_id)
        if self._active_turns.get(turn.session_id) is turn:
            self._active_turns.pop(turn.session_id, None)
        return "terminal"

    async def _discover_durable_turn(self, session_id: str) -> _ActiveTurn | None:
        result = await self._request("sessions.get", SessionsGetParams(session_id=session_id))
        if not isinstance(result, SessionsGetResult) or result.session.session_id != session_id:
            raise _gateway_request_error("invalid_session_result")
        self._known_sessions.add(session_id)
        turn = await self._remember_active_run(result.session)
        if turn is not None:
            return turn
        latest = await self._request(
            "runs.latest",
            RunsLatestParams(session_id=session_id),
        )
        if not isinstance(latest, RunsLatestResult):
            raise _gateway_request_error("invalid_run_result")
        if latest.run is None:
            self._recovery_checked_sessions.add(session_id)
            return None
        return self._remember_run_snapshot(session_id, latest.run)

    async def _remember_active_run(self, snapshot: SessionSnapshot) -> _ActiveTurn | None:
        if snapshot.active_run_id is None:
            return None
        result = await self._request(
            "runs.get",
            RunsGetParams(session_id=snapshot.session_id, run_id=snapshot.active_run_id),
        )
        if (
            not isinstance(result, RunsGetResult)
            or result.run.session_id != snapshot.session_id
            or result.run.run_id != snapshot.active_run_id
        ):
            raise _gateway_request_error("invalid_run_result")
        return self._remember_run_snapshot(snapshot.session_id, result.run)

    def _remember_run_snapshot(
        self,
        session_id: str,
        snapshot: RunSnapshot,
    ) -> _ActiveTurn:
        if snapshot.session_id != session_id:
            raise _gateway_request_error("invalid_run_result")
        current = self._active_turns.get(session_id)
        if current is not None:
            if current.run_id != snapshot.run_id:
                raise _gateway_request_error("session_run_state_conflict")
            return current
        turn = _ActiveTurn(
            session_id=session_id,
            send_params=None,
            send_idempotency_key=None,
            run_id=snapshot.run_id,
            recovery_required=True,
            recovered_from_gateway=True,
        )
        self._active_turns[session_id] = turn
        return turn

    async def _consume_turn_events(
        self,
        turn: _ActiveTurn,
        subscription: GatewayEventSubscriptionProtocol,
    ) -> PromptResponse:
        saw_update = False
        while True:
            try:
                event = await subscription.get()
            except (GatewayRecoveryRequired, GatewayConnectionClosed):
                raise
            except GatewayClientError as exc:
                raise _gateway_request_error(exc.code) from exc
            if not _matches_turn(event, turn):
                continue
            payload = event.payload
            if isinstance(payload, ChatUpdateEvent):
                await self._send_text_update(turn.session_id, payload.text)
                saw_update = True
                continue
            if isinstance(payload, ChatErrorEvent):
                turn.terminal = True
                raise RequestError(
                    -32003,
                    "Gateway chat failed",
                    {"code": payload.code, "retryable": payload.retryable},
                )
            if isinstance(payload, ChatFinalEvent):
                turn.terminal = True
                final_text = _final_text(payload)
                if final_text and not saw_update:
                    await self._send_text_update(turn.session_id, final_text)
                return PromptResponse(
                    stop_reason="cancelled" if payload.stop_reason == "aborted" else "end_turn"
                )

    async def _abort_turn(self, turn: _ActiveTurn) -> None:
        async with turn.abort_lock:
            if turn.abort_requested or turn.terminal or turn.run_id is None:
                return
            result = await self._gateway.request(
                "chat.abort",
                ChatAbortParams(session_id=turn.session_id, run_id=turn.run_id),
                idempotency_key=turn.abort_idempotency_key,
            )
            if (
                not isinstance(result, ChatAbortResult)
                or result.session_id != turn.session_id
                or result.run_id != turn.run_id
            ):
                raise GatewayClientError(
                    "invalid_abort_result",
                    "Gateway abort result is invalid",
                )
            turn.abort_requested = True

    async def _request(
        self,
        method: str,
        params: GatewayRequestParams,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayMethodResult:
        try:
            return await self._gateway.request(
                method,
                params,
                idempotency_key=idempotency_key,
            )
        except GatewayRemoteError as exc:
            raise RequestError(
                -32002,
                "Gateway request was rejected",
                {"code": exc.code},
            ) from exc
        except GatewayClientError as exc:
            raise _gateway_request_error(exc.code) from exc

    async def _send_text_update(self, session_id: str, text: str) -> None:
        conn = getattr(self, "_conn", None)
        if conn is None:
            raise _gateway_request_error("acp_client_not_connected")
        await conn.session_update(
            session_id=session_id,
            update=update_agent_message_text(text),
        )

    def _require_authenticated(self) -> None:
        self._require_local_authentication()
        if not self._gateway.connected:
            raise RequestError.auth_required({"code": "local_gateway_auth_required"})

    def _require_local_authentication(self) -> None:
        if not self._authenticated:
            raise RequestError.auth_required({"code": "local_gateway_auth_required"})


def _reject_external_runtime_context(
    additional_directories: list[str] | None,
    mcp_servers: list[_AcpMcpServer] | None,
) -> None:
    if additional_directories or mcp_servers:
        raise RequestError.invalid_params({"code": "gateway_controls_runtime_context"})


def _text_prompt(prompt: list[_AcpContentBlock]) -> str:
    if not prompt or any(not isinstance(block, TextContentBlock) for block in prompt):
        raise RequestError.invalid_params({"code": "text_prompt_only"})
    text = "\n".join(block.text for block in prompt)
    if not text.strip() or len(text) > MAX_RPC_TEXT_CHARS:
        raise RequestError.invalid_params({"code": "invalid_prompt_text"})
    return text


def _session_modes(snapshot: SessionSnapshot) -> SessionModeState:
    modes = [
        SessionMode(id="default", name="Default"),
        SessionMode(id="debug", name="Debug"),
    ]
    if snapshot.mode not in {mode.id for mode in modes}:
        modes.append(SessionMode(id=snapshot.mode, name=snapshot.mode))
    return SessionModeState(current_mode_id=snapshot.mode, available_modes=modes)


def _parse_acp_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 18:
        raise RequestError.invalid_params({"code": "invalid_session_cursor"})
    return int(cursor)


def _matches_turn(event: TypedGatewayEvent, turn: _ActiveTurn) -> bool:
    payload = event.payload
    return (
        isinstance(payload, (ChatUpdateEvent, ChatFinalEvent, ChatErrorEvent))
        and payload.session_id == turn.session_id
        and payload.run_id == turn.run_id
    )


def _final_text(payload: ChatFinalEvent) -> str:
    if any(not isinstance(segment, TextRpcSegment) for segment in payload.segments):
        raise _gateway_request_error("unsupported_gateway_output")
    text_segments = tuple(
        segment for segment in payload.segments if isinstance(segment, TextRpcSegment)
    )
    return "\n".join(segment.text for segment in text_segments)


def _snapshot_text(snapshot: RunSnapshot) -> str:
    if any(not isinstance(segment, TextRpcSegment) for segment in snapshot.segments):
        raise _gateway_request_error("unsupported_gateway_output")
    return "\n".join(
        segment.text for segment in snapshot.segments if isinstance(segment, TextRpcSegment)
    )


def _gateway_request_error(code: str) -> RequestError:
    return RequestError(-32001, "Gateway unavailable", {"code": code})


async def amain(
    config: GatewayAcpRuntimeConfig,
    input_stream: Any = None,
    output_stream: Any = None,
) -> None:
    client = GatewayWebSocketClient(config.gateway)
    await client.connect()
    agent = GatewayAcpAgent(
        client,
        local_auth_method_id=config.local_auth_method_id,
        implementation_name=config.implementation_name,
        implementation_title=config.implementation_title,
        implementation_version=config.implementation_version,
    )
    try:
        await run_agent(agent, input_stream=input_stream, output_stream=output_stream)
    finally:
        await client.close()


def main(config: GatewayAcpRuntimeConfig) -> int:
    try:
        asyncio.run(amain(config))
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1
    return 0


def config_from_env(
    environ: Mapping[str, str] | None = None,
) -> GatewayAcpRuntimeConfig:
    """Load only the loopback Gateway endpoint and client credential from env."""

    values = os.environ if environ is None else environ
    url = str(values.get("CHATCOPILOT_GATEWAY_URL", "") or "").strip()
    token = str(values.get("CHATCOPILOT_GATEWAY_TOKEN", "") or "").strip()
    if not url:
        raise ValueError("CHATCOPILOT_GATEWAY_URL is required")
    if not token:
        raise ValueError("CHATCOPILOT_GATEWAY_TOKEN is required")
    return GatewayAcpRuntimeConfig(
        gateway=GatewayClientConfig(url=url, token=token),
    )


def main_from_env() -> int:
    try:
        config = config_from_env()
    except ValueError:
        return 2
    return main(config)


__all__ = [
    "GatewayAcpAgent",
    "GatewayAcpRuntimeConfig",
    "LOCAL_AUTH_METHOD_ID",
    "amain",
    "config_from_env",
    "main",
    "main_from_env",
]
