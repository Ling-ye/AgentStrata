"""Authorized turn coordination for Gateway clients and trusted Channel ingress."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import math
from pathlib import Path
import re
import time
from typing import Protocol, cast

from chatcopilot.application.actor_runtime import (
    ActorTurnOutcome,
    ActorTurnRequest,
)
from chatcopilot.application.resources import (
    ResourceMaterializationError,
    ResourceMaterializationService,
)
from chatcopilot.application.sessions import SessionManagerError
from chatcopilot.application.workspaces import build_actor_workspace
from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
from chatcopilot.channels.base import ChannelDeliveryError
from chatcopilot.contracts.agent import AgentEvent, ResourceRef, TextDelta
from chatcopilot.contracts.authorization import (
    AuthorizationDecision,
    AuthorizationOperation,
    AuthorizationRequest,
    Principal,
    stable_payload_digest,
)
from chatcopilot.contracts.cancellation import CancellationProbe, CancellationToken
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    MessageSegment,
    OutboundEnvelope,
)
from chatcopilot.contracts.gateway_rpc import (
    ChatAbortResult,
    ChatErrorEvent,
    ChatFinalEvent,
    ChatSendResult,
    ChatUpdateEvent,
    TextRpcSegment,
)
from chatcopilot.contracts.identity import ConversationIdentity, TurnIdentity

from .application import GatewayApplicationError, GatewaySessionService
from .events import GatewayEventPublisher
from .server import GatewayClientContext, GatewayDispatchError
from .state_store import (
    GatewayStateStore,
    RunConflict,
    StaleWriterGeneration,
)


_MAX_EVENT_TEXT = 64 * 1024
_MAX_STREAM_TEXT = 1024 * 1024
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ActorTurnExecutorPort(Protocol):
    async def execute(
        self,
        request: ActorTurnRequest,
        *,
        on_event: Callable[[AgentEvent], None],
        cancellation: CancellationProbe | None = None,
    ) -> ActorTurnOutcome: ...

    def commit_exchange(
        self,
        request: ActorTurnRequest,
        outcome: ActorTurnOutcome,
        *,
        exchange_id: str | None = None,
    ) -> ActorTurnOutcome: ...

    def discard_exchange(
        self,
        request: ActorTurnRequest,
        outcome: ActorTurnOutcome,
    ) -> None: ...


class ChannelOutboundPort(Protocol):
    async def send(self, envelope: OutboundEnvelope) -> object: ...


class GatewayTurnCoordinatorError(RuntimeError):
    """Secret-free application failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _AgentEventForwarder:
    def __init__(
        self,
        *,
        events: GatewayEventPublisher,
        session_id: str,
        run_id: str,
        cancellation: CancellationToken,
    ) -> None:
        self._events = events
        self._session_id = session_id
        self._run_id = run_id
        self._cancellation = cancellation
        self._forwarded_chars = 0

    def __call__(self, event: AgentEvent) -> None:
        self._cancellation.raise_if_cancelled()
        if not isinstance(event, TextDelta) or not event.text:
            return
        remaining = _MAX_STREAM_TEXT - self._forwarded_chars
        if remaining <= 0:
            return
        text = event.text[:remaining]
        self._forwarded_chars += len(text)
        for offset in range(0, len(text), _MAX_EVENT_TEXT):
            chunk = text[offset : offset + _MAX_EVENT_TEXT]
            if chunk:
                self._events.emit(
                    "chat.update",
                    ChatUpdateEvent(
                        session_id=self._session_id,
                        run_id=self._run_id,
                        text=chunk,
                    ),
                    session_id=self._session_id,
                )


class GatewayTurnCoordinator:
    """Own run lifecycle, cancellation, Agent execution, and Channel delivery."""

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        sessions: GatewaySessionService,
        events: GatewayEventPublisher,
        actor_executor: ActorTurnExecutorPort,
        identity_policy: IdentityPolicy,
        admission_policy: AdmissionPolicy,
        generation: int,
        workspace_root: Path,
        resource_materializer: ResourceMaterializationService | None = None,
        channel_runtime: ChannelOutboundPort | None = None,
        on_admission_decision: Callable[[AuthorizationDecision], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if generation != sessions.generation:
            raise ValueError("coordinator generation does not match session service")
        self._state_store = state_store
        self._sessions = sessions
        self._events = events
        self._actor_executor = actor_executor
        self._identity_policy = identity_policy
        self._admission_policy = admission_policy
        self._generation = generation
        self._workspace_root = Path(workspace_root)
        self._resource_materializer = resource_materializer
        self._channel_runtime = channel_runtime
        self._on_admission_decision = on_admission_decision
        self._clock = clock
        self._tokens: dict[str, CancellationToken] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    def set_channel_runtime(self, runtime: ChannelOutboundPort) -> None:
        if self._channel_runtime is not None and self._channel_runtime is not runtime:
            raise RuntimeError("Gateway Channel runtime is already attached")
        self._channel_runtime = runtime

    @property
    def active_task_count(self) -> int:
        return len(self._tasks)

    async def submit_client_turn(
        self,
        *,
        client: GatewayClientContext,
        session_id: str,
        segments: tuple[object, ...],
        message_id: str | None,
        request_id: str,
        idempotency_key: str,
    ) -> ChatSendResult:
        if self._closing:
            raise GatewayDispatchError("gateway_stopping", "Gateway is stopping")
        session = self._sessions.get_visible(client=client, session_id=session_id)
        principal = self._sessions.principal_for_client(client=client, session=session)
        canonical_text = _client_text(segments)
        run_id = _client_run_id(
            client_id=client.client_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
        token = CancellationToken()
        self._begin_run(
            session_id=session_id,
            run_id=run_id,
            input_fingerprint=_input_fingerprint(
                canonical_text=canonical_text,
                message_id=message_id,
                principal=principal,
            ),
        )
        self._tokens[run_id] = token
        task = asyncio.create_task(
            self._execute_client_run(
                session_id=session_id,
                run_id=run_id,
                principal=principal,
                canonical_text=canonical_text,
                message_id=message_id,
                request_id=request_id,
                cancellation=token,
            ),
            name=f"gateway-client-run:{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed: self._task_finished(run_id, completed))
        return ChatSendResult(session_id=session_id, run_id=run_id)

    async def abort(
        self,
        *,
        client: GatewayClientContext,
        session_id: str,
        run_id: str,
    ) -> ChatAbortResult:
        self._sessions.get_visible(client=client, session_id=session_id)
        run = self._state_store.get_run(run_id)
        if run is None or run.session_id != session_id:
            raise GatewayDispatchError("run_not_found", "Gateway run does not exist")
        if run.state in {"completed", "aborted", "failed"}:
            return ChatAbortResult(session_id=session_id, run_id=run_id, aborted=False)
        try:
            updated = self._state_store.request_abort(
                generation=self._generation,
                session_id=session_id,
                run_id=run_id,
                now=self._now(),
            )
        except RunConflict as exc:
            raise GatewayDispatchError(
                "run_abort_conflict",
                "Gateway run cannot be aborted from its current state",
            ) from exc
        token = self._tokens.get(run_id)
        if token is None:
            raise GatewayDispatchError(
                "run_recovery_required",
                "Gateway run has no active worker and requires recovery",
            )
        token.cancel()
        return ChatAbortResult(
            session_id=session_id,
            run_id=run_id,
            aborted=updated.state == "abort_requested",
        )

    def authorize_inbound(self, event: CanonicalInboundEvent) -> Principal:
        """Derive and admit a Principal without persisting the untrusted event payload."""

        if self._closing:
            raise GatewayTurnCoordinatorError("gateway_stopping", "Gateway is stopping")
        return self._authorize_inbound(event)

    async def handle_authorized_inbound(
        self,
        event: CanonicalInboundEvent,
        principal: Principal,
    ) -> None:
        """Execute one already-admitted event without repeating identity or admission policy."""

        if self._closing:
            raise GatewayTurnCoordinatorError("gateway_stopping", "Gateway is stopping")
        _assert_principal_event_binding(principal, event)
        session = self._sessions.ensure_channel_session(
            account=event.evidence.account,
            conversation=event.evidence.conversation,
        )
        run_id = _channel_run_id(event)
        canonical_text = _inbound_text(event)
        token = CancellationToken()
        self._begin_run(
            session_id=session.session_id,
            run_id=run_id,
            input_fingerprint=_input_fingerprint(
                canonical_text=canonical_text,
                message_id=event.evidence.message_id,
                principal=principal,
            ),
        )
        self._tokens[run_id] = token
        try:
            resources = await self._materialize_resources(event, principal)
            task = asyncio.create_task(
                self._execute_channel_run(
                    event=event,
                    session_id=session.session_id,
                    run_id=run_id,
                    principal=principal,
                    canonical_text=canonical_text,
                    resources=resources,
                    cancellation=token,
                ),
                name=f"gateway-channel-run:{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(
                lambda completed: self._task_finished(run_id, completed)
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                token.cancel()
                await task
                raise
        except StaleWriterGeneration:
            raise
        except Exception as exc:
            current = self._state_store.get_run(run_id)
            if current is not None and current.state not in {"completed", "aborted", "failed"}:
                self._fail_run(session_id=session.session_id, run_id=run_id, error=exc)
            raise
        finally:
            self._tokens.pop(run_id, None)

    async def close(self) -> None:
        """Request durable cancellation and await all detached Gateway-client runs."""

        self._closing = True
        for run_id, token in tuple(self._tokens.items()):
            run = self._state_store.get_run(run_id)
            if run is not None and run.state in {"accepted", "running"}:
                try:
                    self._state_store.request_abort(
                        generation=self._generation,
                        session_id=run.session_id,
                        run_id=run_id,
                        now=self._now(),
                    )
                except (RunConflict, StaleWriterGeneration):
                    pass
            token.cancel()
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _authorize_inbound(self, event: CanonicalInboundEvent) -> Principal:
        evidence = event.evidence
        conversation = ConversationIdentity(
            platform=evidence.account.channel,
            chat_kind=evidence.conversation.kind,
            chat_id=evidence.conversation.conversation_id,
        )
        turn = TurnIdentity(
            conversation=conversation,
            sender_user_id=evidence.sender.sender_id,
            sender_user_name=evidence.sender.display_name,
            message_id=evidence.message_id,
            source="gateway-authenticated-channel",
        )
        principal = self._identity_policy.principal(
            turn=turn,
            channel=evidence.account.channel,
            account_id=evidence.account.account_id,
            evidence_digest=_event_evidence_digest(event),
        )
        request = AuthorizationRequest(
            request_id="ingress_" + hashlib.sha256(
                (evidence.account.channel + "\0" + evidence.event_id).encode("utf-8")
            ).hexdigest()[:24],
            principal=principal,
            operation=AuthorizationOperation.INGRESS,
            target="channel-message",
            params_digest=stable_payload_digest(
                {
                    "conversation_kind": evidence.conversation.kind,
                    "conversation_id": evidence.conversation.conversation_id,
                    "event_id": evidence.event_id,
                    "message_id": evidence.message_id,
                }
            ),
        )
        decision = self._admission_policy.decide(request)
        if self._on_admission_decision is not None:
            self._on_admission_decision(decision)
        if not decision.allowed:
            raise GatewayTurnCoordinatorError(
                decision.code,
                "Channel ingress is not admitted",
            )
        return principal

    async def _materialize_resources(
        self,
        event: CanonicalInboundEvent,
        principal: Principal,
    ) -> tuple[ResourceRef, ...]:
        if not event.resource_tickets:
            return ()
        if self._resource_materializer is None:
            raise GatewayTurnCoordinatorError(
                "resource_materializer_unavailable",
                "Inbound resources cannot be materialized",
            )
        try:
            binding = build_actor_workspace(
                workspace_root=self._workspace_root,
                principal=principal,
            )
            return await self._resource_materializer.materialize(
                event=event,
                actor_id=principal.user_id,
                workspace=binding.workspace,
                now=self._now(),
            )
        except ResourceMaterializationError as exc:
            raise GatewayTurnCoordinatorError(exc.code, str(exc)) from exc
        except Exception as exc:
            raise GatewayTurnCoordinatorError(
                "resource_materialization_failed",
                "Inbound resources could not be materialized",
            ) from exc

    async def _execute_client_run(
        self,
        *,
        session_id: str,
        run_id: str,
        principal: Principal,
        canonical_text: str,
        message_id: str | None,
        request_id: str,
        cancellation: CancellationToken,
    ) -> None:
        try:
            result = await self._execute_actor(
                request=ActorTurnRequest(
                    session_id=session_id,
                    principal=principal,
                    canonical_text=canonical_text,
                    message_id=message_id,
                    metadata={"gateway_request_id": request_id},
                ),
                run_id=run_id,
                cancellation=cancellation,
            )
            await self._complete_without_channel(
                session_id=session_id,
                run_id=run_id,
                result=result,
            )
        except StaleWriterGeneration:
            return
        except Exception as exc:
            self._fail_run(session_id=session_id, run_id=run_id, error=exc)

    async def _execute_channel_run(
        self,
        *,
        event: CanonicalInboundEvent,
        session_id: str,
        run_id: str,
        principal: Principal,
        canonical_text: str,
        resources: tuple[ResourceRef, ...],
        cancellation: CancellationToken,
    ) -> None:
        request: ActorTurnRequest | None = None
        result: ActorTurnOutcome | None = None
        exchange_committed = False
        try:
            request = ActorTurnRequest(
                session_id=session_id,
                principal=principal,
                canonical_text=canonical_text,
                message_id=event.evidence.message_id,
                resource_refs=resources,
                metadata={"gateway_event_id": event.evidence.event_id},
                sender_display_name=event.evidence.sender.display_name,
            )
            result = await self._execute_actor(
                request=request,
                run_id=run_id,
                cancellation=cancellation,
            )
            if result.result.stop_reason == "cancelled":
                self._actor_executor.discard_exchange(request, result)
                self._finish_aborted(session_id=session_id, run_id=run_id)
                return
            final_text = _bounded_final_text(result.result.final_text)
            runtime = self._channel_runtime
            if runtime is None:
                raise GatewayTurnCoordinatorError(
                    "channel_runtime_unavailable",
                    "Gateway Channel runtime is unavailable",
                )
            if final_text:
                envelope = OutboundEnvelope(
                    outbound_id=_outbound_id(run_id),
                    account=event.evidence.account,
                    conversation=event.evidence.conversation,
                    segments=(MessageSegment(kind="text", text=final_text),),
                    created_at=self._now(),
                    session_id=session_id,
                    run_id=run_id,
                    reply_to_message_id=event.evidence.message_id,
                )
                await runtime.send(envelope)
                result = self._actor_executor.commit_exchange(
                    request,
                    result,
                    exchange_id=envelope.outbound_id,
                )
                exchange_committed = True
            else:
                self._actor_executor.discard_exchange(request, result)
            self._finish_completed(
                session_id=session_id,
                run_id=run_id,
                final_text=final_text,
            )
        except StaleWriterGeneration:
            if request is not None and result is not None and not exchange_committed:
                self._actor_executor.discard_exchange(request, result)
            raise
        except Exception as error:
            failure = error
            if request is not None and result is not None and not exchange_committed:
                try:
                    self._actor_executor.discard_exchange(request, result)
                except Exception as discard_error:
                    failure = discard_error
            self._fail_run(session_id=session_id, run_id=run_id, error=failure)
            raise

    async def _execute_actor(
        self,
        *,
        request: ActorTurnRequest,
        run_id: str,
        cancellation: CancellationToken,
    ) -> ActorTurnOutcome:
        session_id = request.session_id
        run = self._state_store.get_run(run_id)
        if run is None:
            raise GatewayTurnCoordinatorError("run_not_found", "Gateway run does not exist")
        if cancellation.is_cancelled or run.state == "abort_requested":
            cancellation.cancel()
            self._finish_aborted(session_id=session_id, run_id=run_id)
            return cast(ActorTurnOutcome, _CancelledOutcome())
        self._state_store.start_run(
            generation=self._generation,
            session_id=session_id,
            run_id=run_id,
            now=self._now(),
        )
        forwarder = _AgentEventForwarder(
            events=self._events,
            session_id=session_id,
            run_id=run_id,
            cancellation=cancellation,
        )
        return await self._actor_executor.execute(
            request,
            on_event=forwarder,
            cancellation=cancellation,
        )

    async def _complete_without_channel(
        self,
        *,
        session_id: str,
        run_id: str,
        result: ActorTurnOutcome,
    ) -> None:
        if result.result.stop_reason == "cancelled":
            self._finish_aborted(session_id=session_id, run_id=run_id)
            return
        final_text = _bounded_final_text(result.result.final_text)
        self._finish_completed(
            session_id=session_id,
            run_id=run_id,
            final_text=final_text,
        )

    def _begin_run(self, *, session_id: str, run_id: str, input_fingerprint: str) -> None:
        try:
            self._state_store.begin_run(
                generation=self._generation,
                session_id=session_id,
                run_id=run_id,
                input_fingerprint=input_fingerprint,
                now=self._now(),
            )
            self._sessions.session_manager.begin_run(
                session_id,
                run_id,
                generation=self._generation,
            )
        except (RunConflict, SessionManagerError) as exc:
            raise GatewayDispatchError(
                "session_run_active",
                "Gateway session already has an active run",
            ) from exc

    def _finish_completed(self, *, session_id: str, run_id: str, final_text: str) -> None:
        self._state_store.finish_run(
            generation=self._generation,
            session_id=session_id,
            run_id=run_id,
            outcome="completed",
            result={"final_text": final_text, "stop_reason": "completed"},
            now=self._now(),
        )
        self._sessions.session_manager.finish_run(
            session_id,
            run_id,
            generation=self._generation,
        )
        segments = (TextRpcSegment(final_text),) if final_text else ()
        self._events.emit(
            "chat.final",
            ChatFinalEvent(
                session_id=session_id,
                run_id=run_id,
                stop_reason="completed",
                segments=segments,
            ),
            session_id=session_id,
        )

    def _finish_aborted(self, *, session_id: str, run_id: str) -> None:
        current = self._state_store.get_run(run_id)
        if current is None:
            return
        if current.state in {"completed", "aborted", "failed"}:
            return
        if current.state in {"accepted", "running"}:
            self._state_store.request_abort(
                generation=self._generation,
                session_id=session_id,
                run_id=run_id,
                now=self._now(),
            )
        self._state_store.finish_run(
            generation=self._generation,
            session_id=session_id,
            run_id=run_id,
            outcome="aborted",
            result={"final_text": "", "stop_reason": "cancelled"},
            worker_stop_reason="cancelled",
            now=self._now(),
        )
        self._sessions.session_manager.finish_run(
            session_id,
            run_id,
            generation=self._generation,
        )
        self._events.emit(
            "chat.final",
            ChatFinalEvent(
                session_id=session_id,
                run_id=run_id,
                stop_reason="aborted",
                segments=(),
            ),
            session_id=session_id,
        )

    def _fail_run(self, *, session_id: str, run_id: str, error: Exception) -> None:
        current = self._state_store.get_run(run_id)
        if current is None or current.state in {"completed", "aborted", "failed"}:
            return
        code = _error_code(error)
        try:
            self._state_store.finish_run(
                generation=self._generation,
                session_id=session_id,
                run_id=run_id,
                outcome="failed",
                error_code=code,
                now=self._now(),
            )
            self._sessions.session_manager.finish_run(
                session_id,
                run_id,
                generation=self._generation,
            )
            self._events.emit(
                "chat.error",
                ChatErrorEvent(
                    session_id=session_id,
                    run_id=run_id,
                    code=code,
                    message="Gateway turn failed",
                    retryable=False,
                ),
                session_id=session_id,
            )
        except StaleWriterGeneration:
            return

    def _task_finished(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self._tokens.pop(run_id, None)
        if not task.cancelled():
            task.exception()

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise GatewayTurnCoordinatorError("gateway_clock_invalid", "Gateway clock is invalid")
        return float(value)


class _CancelledResult:
    stop_reason = "cancelled"
    final_text = ""


class _CancelledOutcome:
    result = _CancelledResult()


def _client_text(segments: tuple[object, ...]) -> str:
    texts: list[str] = []
    for segment in segments:
        if isinstance(segment, TextRpcSegment):
            texts.append(segment.text)
        else:
            raise GatewayDispatchError(
                "client_resource_unsupported",
                "Gateway client resource and reply segments are not supported by this runtime",
            )
    text = "".join(texts)
    if not text.strip():
        raise GatewayDispatchError("chat_text_required", "Gateway chat input requires text")
    return text


def _event_evidence_digest(event: CanonicalInboundEvent) -> str:
    evidence = event.evidence
    return stable_payload_digest(
        {
            "account": [evidence.account.channel, evidence.account.account_id],
            "conversation": [
                evidence.conversation.kind,
                evidence.conversation.conversation_id,
            ],
            "event_id": evidence.event_id,
            "frame_sha256": evidence.frame_sha256,
            "sender_id": evidence.sender.sender_id,
        }
    )


def _assert_principal_event_binding(
    principal: Principal,
    event: CanonicalInboundEvent,
) -> None:
    evidence = event.evidence
    conversation = principal.conversation
    if (
        principal.channel != evidence.account.channel
        or principal.account_id != evidence.account.account_id
        or conversation.platform != evidence.account.channel
        or conversation.chat_kind != evidence.conversation.kind
        or conversation.chat_id != evidence.conversation.conversation_id
        or principal.user_id != evidence.sender.sender_id
        or principal.evidence_digest != _event_evidence_digest(event)
    ):
        raise GatewayTurnCoordinatorError(
            "ingress_principal_mismatch",
            "Authorized Principal does not match the Channel event",
        )


def _inbound_text(event: CanonicalInboundEvent) -> str:
    parts: list[str] = []
    for segment in event.segments:
        if segment.kind == "text" and segment.text:
            parts.append(segment.text)
        elif segment.kind == "mention" and segment.target != event.evidence.account.account_id:
            parts.append(f"@{segment.target}")
    text = "".join(parts).strip()
    if text:
        return text
    if event.resource_tickets:
        return "请处理本次消息中的附件。"
    raise GatewayTurnCoordinatorError("message_text_empty", "Channel message has no usable content")


def _input_fingerprint(
    *,
    canonical_text: str,
    message_id: str | None,
    principal: Principal,
) -> str:
    return hashlib.sha256(
        (
            principal.actor_ref
            + "\0"
            + canonical_text
            + "\0"
            + str(message_id or "")
        ).encode("utf-8")
    ).hexdigest()


def _client_run_id(*, client_id: str, session_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        (client_id + "\0" + session_id + "\0" + idempotency_key).encode("utf-8")
    ).hexdigest()[:32]
    return "run_" + digest


def _channel_run_id(event: CanonicalInboundEvent) -> str:
    evidence = event.evidence
    digest = hashlib.sha256(
        (
            evidence.account.channel
            + "\0"
            + evidence.account.account_id
            + "\0"
            + evidence.event_id
        ).encode("utf-8")
    ).hexdigest()[:32]
    return "run_" + digest


def _outbound_id(run_id: str) -> str:
    return "outbound_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


def _bounded_final_text(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_EVENT_TEXT:
        raise GatewayTurnCoordinatorError(
            "agent_output_invalid",
            "Agent output is invalid or exceeds the Gateway limit",
        )
    return value


def _error_code(error: Exception) -> str:
    candidate = getattr(error, "code", None)
    if isinstance(candidate, str) and _ERROR_CODE_RE.fullmatch(candidate):
        return candidate
    if isinstance(error, ChannelDeliveryError):
        return error.code
    if isinstance(error, ResourceMaterializationError):
        return error.code
    if isinstance(error, GatewayApplicationError):
        return error.code
    return "gateway_turn_failed"


__all__ = [
    "ActorTurnExecutorPort",
    "ChannelOutboundPort",
    "GatewayTurnCoordinator",
    "GatewayTurnCoordinatorError",
]
