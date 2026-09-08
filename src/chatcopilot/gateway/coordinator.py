"""Authorized turn coordination for Gateway clients and trusted Channel ingress."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
import hashlib
import math
import re
import time
from typing import Protocol

from chatcopilot.application.sessions import SessionManagerError
from chatcopilot.contracts.turns import PreparedTurn, TurnOutcome
from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
from chatcopilot.channels.base import ChannelDeliveryError
from chatcopilot.contracts.agent import AgentEvent, AgentResult, TextDelta
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
    DeliveryReceipt,
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
from chatcopilot.core.runtime_observation import (
    channel_input_summary, inbound_summary, result_summary, runtime_stage, turn_summary,
)

from .application import GatewayApplicationError, GatewaySessionService
from .events import GatewayEventPublisher
from .observations import ACTOR_SPAN_ID, RunObserver, response_outbound_id
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
        request: PreparedTurn,
        *,
        on_event: Callable[[AgentEvent], None],
        cancellation: CancellationProbe | None = None,
    ) -> TurnOutcome: ...

    def prepare_client(self, *, session_id: str, run_id: str, principal: Principal,
                       canonical_text: str, message_id: str | None, request_id: str) -> PreparedTurn: ...

    async def prepare_channel(self, *, event: CanonicalInboundEvent, principal: Principal,
                              session_id: str, run_id: str, canonical_text: str,
                              now: float) -> PreparedTurn: ...

    def commit_exchange(self, request: PreparedTurn, outcome: TurnOutcome, *,
                        envelope: OutboundEnvelope, receipt: DeliveryReceipt) -> TurnOutcome: ...

    def discard_exchange(self, request: PreparedTurn, outcome: TurnOutcome) -> None: ...

    def close(self) -> None: ...


class ChannelOutboundPort(Protocol):
    async def send(self, envelope: OutboundEnvelope) -> DeliveryReceipt: ...


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
        observer: RunObserver,
    ) -> None:
        self._events = events
        self._session_id = session_id
        self._run_id = run_id
        self._cancellation = cancellation
        self._forwarded_chars = 0
        self._observer = observer

    def __call__(self, event: AgentEvent) -> None:
        self._cancellation.raise_if_cancelled()
        self._observer(event)
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
        observer = RunObserver(self._state_store, self._generation, run_id)
        observer.accepted(canonical_text, principal.role.value)
        with observer.scope():
            with runtime_stage("gateway.accept", "gateway", trace_id=run_id,
                               input={"text": canonical_text, "request_id": request_id},
                               source="gateway", target="application", entrypoint="client",
                               duration_recorded=False) as stage:
                stage.complete({"run_id": run_id, "session_id": session_id, "role": principal.role.value,
                                "admission": "allowed", "channel": "not_traversed"})
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
        observer = RunObserver(self._state_store, self._generation, run_id)
        observer.accepted(canonical_text, principal.role.value)
        try:
            with observer.scope():
                native = channel_input_summary(event)
                with runtime_stage("channel.receive", "channel", trace_id=run_id, input=native,
                                   source="channel", target="gateway", event_id=event.evidence.event_id,
                                   occurred_at=event.evidence.observed_at, duration_recorded=False) as stage:
                    stage.complete(inbound_summary(event))
                with runtime_stage("gateway.accept", "gateway", trace_id=run_id,
                                   input={"text": canonical_text, "event_id": event.evidence.event_id},
                                   source="channel", target="application", duration_recorded=False,
                                   entrypoint="channel") as stage:
                    stage.complete({"run_id": run_id, "session_id": session.session_id,
                                    "role": principal.role.value, "admission": "allowed",
                                    "policy_version": self._admission_policy.policy_version})
                with runtime_stage("application.prepare", "application", trace_id=run_id,
                                   input={"text": canonical_text, "resources": inbound_summary(event).get("resources", [])},
                                   source="gateway", target="application") as stage:
                    request = await self._actor_executor.prepare_channel(
                        event=event, principal=principal, session_id=session.session_id,
                        run_id=run_id, canonical_text=canonical_text, now=self._now())
                    stage.complete(turn_summary(request), resource_count=len(request.resource_refs))
                request = replace(request, metadata={**(request.metadata or {}),
                                  "trace_id": run_id, "parent_span_id": ACTOR_SPAN_ID})
                task = asyncio.create_task(
                    self._execute_channel_run(
                        event=event,
                        session_id=session.session_id,
                        run_id=run_id,
                        principal=principal,
                        request=request,
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
        self._actor_executor.close()

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
        observer = RunObserver(self._state_store, self._generation, run_id)
        with observer.scope():
            with runtime_stage("application.prepare", "application", trace_id=run_id,
                               input={"text": canonical_text}, source="gateway", target="application") as stage:
                request = self._actor_executor.prepare_client(
                    run_id=run_id, session_id=session_id, principal=principal,
                    canonical_text=canonical_text, message_id=message_id, request_id=request_id)
                stage.complete(turn_summary(request), resource_count=len(request.resource_refs))
            request = replace(request, metadata={**(request.metadata or {}),
                              "trace_id": run_id, "parent_span_id": ACTOR_SPAN_ID})
            result = None
            try:
                result = await self._execute_actor(
                    request=request,
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
            finally:
                if result is not None:
                    self._actor_executor.discard_exchange(request, result)

    async def _execute_channel_run(
        self,
        *,
        event: CanonicalInboundEvent,
        session_id: str,
        run_id: str,
        principal: Principal,
        request: PreparedTurn,
        cancellation: CancellationToken,
    ) -> None:
        result: TurnOutcome | None = None
        exchange_committed = False
        try:
            result = await self._execute_actor(
                request=request,
                run_id=run_id,
                cancellation=cancellation,
            )
            if result.result.stop_reason == "cancelled" or cancellation.is_cancelled:
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
                    outbound_id=response_outbound_id(run_id),
                    account=event.evidence.account,
                    conversation=event.evidence.conversation,
                    segments=(MessageSegment(kind="text", text=final_text),),
                    created_at=self._now(),
                    session_id=session_id,
                    run_id=run_id,
                    reply_to_message_id=event.evidence.message_id,
                )
                receipt = await runtime.send(envelope)
                self._sessions.assert_current_generation()
                if cancellation.is_cancelled:
                    self._actor_executor.discard_exchange(request, result)
                    self._finish_aborted(session_id=session_id, run_id=run_id)
                    return
                result = self._actor_executor.commit_exchange(
                    request, result, envelope=envelope, receipt=receipt)

                exchange_committed = True
            else:
                self._actor_executor.discard_exchange(request, result)
            self._finish_completed(
                session_id=session_id,
                run_id=run_id,
                final_text=final_text,
            )
        except (StaleWriterGeneration, asyncio.CancelledError):
            if result is not None and not exchange_committed:
                self._actor_executor.discard_exchange(request, result)
            raise
        except Exception as error:
            failure = error
            if result is not None and not exchange_committed:
                try:
                    self._actor_executor.discard_exchange(request, result)
                except Exception as discard_error:
                    failure = discard_error
            self._fail_run(session_id=session_id, run_id=run_id, error=failure)
            raise

    async def _execute_actor(
        self,
        *,
        request: PreparedTurn,
        run_id: str,
        cancellation: CancellationToken,
    ) -> TurnOutcome:
        session_id = request.session_id
        run = self._state_store.get_run(run_id)
        if run is None:
            raise GatewayTurnCoordinatorError("run_not_found", "Gateway run does not exist")
        if cancellation.is_cancelled or run.state == "abort_requested":
            cancellation.cancel()
            self._finish_aborted(session_id=session_id, run_id=run_id)
            return TurnOutcome(AgentResult("", "cancelled"))
        self._state_store.start_run(
            generation=self._generation,
            session_id=session_id,
            run_id=run_id,
            now=self._now(),
        )
        observer = RunObserver(self._state_store, self._generation, run_id, agent_stage_span_id=ACTOR_SPAN_ID)
        forwarder = _AgentEventForwarder(
            events=self._events,
            session_id=session_id,
            run_id=run_id,
            cancellation=cancellation,
            observer=observer,
        )
        observer.prepare(request)
        with observer.scope():
            result = await self._actor_executor.execute(
                request,
                on_event=forwarder,
                cancellation=cancellation,
            )
        with observer.scope():
            with runtime_stage("application.result", "application", trace_id=run_id,
                               input=result_summary(result.result), source="agent", target="gateway",
                               duration_recorded=False) as stage:
                stage.complete({**result_summary(result.result), "exchange_pending": result.exchange is not None},
                               stop_reason=result.result.stop_reason)
        return result

    async def _complete_without_channel(
        self,
        *,
        session_id: str,
        run_id: str,
        result: TurnOutcome,
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
        self._observe_terminal(run_id, "completed")
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
        self._observe_terminal(run_id, "aborted")
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
            self._observe_terminal(run_id, "failed", code=code)
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

    def _observe_terminal(self, run_id: str, state: str, *, code: str | None = None) -> None:
        observer = RunObserver(self._state_store, self._generation, run_id)
        with observer.scope():
            with runtime_stage("gateway.finish", "gateway", trace_id=run_id,
                               input={"run_id": run_id}, duration_recorded=False,
                               source="gateway", target="gateway") as stage:
                stage.complete({"state": state, "error_code": code},
                               status="succeeded" if state == "completed" else state,
                               code=code, run_state=state)

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
    if isinstance(error, GatewayApplicationError):
        return error.code
    return "gateway_turn_failed"


__all__ = [
    "ActorTurnExecutorPort",
    "ChannelOutboundPort",
    "GatewayTurnCoordinator",
    "GatewayTurnCoordinatorError",
]
