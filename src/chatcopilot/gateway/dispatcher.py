"""Typed authenticated Gateway RPC dispatcher."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
from typing import Any, cast

from chatcopilot.contracts.gateway_protocol import EventFrame, RequestFrame
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.gateway_rpc import (
    ApprovalsListParams,
    ApprovalsListResult,
    ApprovalsResolveParams,
    ApprovalsResolveResult,
    ChannelConnectionState,
    ChannelSnapshot,
    ChannelsListParams,
    ChannelsListResult,
    ChatAbortParams,
    ChatRunState,
    ChatSendParams,
    ChatSendResult,
    DeliveriesGetParams,
    DeliveriesGetResult,
    DeliveryReceiptSnapshot,
    DeliverySnapshot,
    EventsReplayParams,
    EventsReplayResult,
    GatewayMethodResult,
    GatewayReplayItem,
    HealthParams,
    HealthResult,
    RunSnapshot,
    RunsGetParams,
    RunsGetResult,
    RunsLatestParams,
    RunsLatestResult,
    SessionUpdatedEvent,
    SessionsCreateParams,
    SessionsCreateResult,
    SessionsGetParams,
    SessionsGetResult,
    SessionsListParams,
    SessionsListResult,
    SessionsPatchParams,
    SessionsPatchResult,
    StatusParams,
    StatusResult,
    TextRpcSegment,
)

from .application import (
    GatewayApplicationError,
    GatewaySessionService,
    client_account,
    conversation_authority_ref,
    is_gateway_admin,
)
from .approvals import GatewayApprovalService
from .channels import ChannelRuntimeManager
from .coordinator import GatewayTurnCoordinator, GatewayTurnCoordinatorError
from .events import GatewayEventPublisher, GatewaySessionEventVisibility
from .protocol import GatewayProtocolError, events_for_scopes
from .rpc_validation import (
    parse_event_payload,
    parse_request_params,
    serialize_method_result,
)
from .server import (
    GatewayClientContext,
    GatewayDispatchError,
    GatewayMutationReconciliation,
)
from .state_store import (
    GatewayStateError,
    GatewayStateStore,
    OutboundRecord,
    RunRecord,
    SessionRecord,
    StaleWriterGeneration,
)


class GatewayApplicationDispatcher:
    """Map closed-schema RPC into one durable application runtime."""

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        sessions: GatewaySessionService,
        events: GatewayEventPublisher,
        coordinator: GatewayTurnCoordinator,
        channel_runtime: ChannelRuntimeManager,
        approval_service: GatewayApprovalService,
        event_visibility: GatewaySessionEventVisibility,
        generation: int,
        ready: Callable[[], bool] | None = None,
    ) -> None:
        if generation != sessions.generation:
            raise ValueError("dispatcher generation does not match session service")
        self._state_store = state_store
        self._sessions = sessions
        self._events = events
        self._coordinator = coordinator
        self._channel_runtime = channel_runtime
        self._approval_service = approval_service
        self._event_visibility = event_visibility
        self._generation = generation
        self._ready = ready or self._default_ready

    async def dispatch(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> Mapping[str, Any]:
        try:
            self._sessions.assert_current_generation()
            params = parse_request_params(request.method, request.params)
            result = await self._dispatch_typed(request, params=params, client=client)
            return serialize_method_result(request.method, result)
        except GatewayDispatchError:
            raise
        except GatewayProtocolError as exc:
            raise GatewayDispatchError(exc.code, str(exc)) from exc
        except StaleWriterGeneration as exc:
            raise GatewayDispatchError(
                "gateway_generation_stale",
                "Gateway writer generation is no longer current",
            ) from exc
        except GatewayApplicationError as exc:
            raise GatewayDispatchError(exc.code, str(exc)) from exc
        except GatewayTurnCoordinatorError as exc:
            raise GatewayDispatchError(exc.code, str(exc)) from exc
        except GatewayStateError as exc:
            raise GatewayDispatchError(
                "state_unavailable",
                "Gateway durable state is unavailable",
            ) from exc

    async def close(self) -> None:
        await self._coordinator.close()

    async def reconcile_mutation(
        self,
        request: RequestFrame,
        *,
        client: GatewayClientContext,
    ) -> GatewayMutationReconciliation:
        """Rebuild an accepted chat receipt only from its deterministic durable run."""

        self._sessions.assert_current_generation()
        if request.method != "chat.send":
            raise GatewayDispatchError(
                "mutation_reconciliation_unsupported",
                "Gateway mutation cannot be reconciled",
            )
        try:
            params = parse_request_params(request.method, request.params)
        except GatewayProtocolError as exc:
            raise GatewayDispatchError(exc.code, str(exc)) from exc
        if not isinstance(params, ChatSendParams):
            raise GatewayDispatchError(
                "mutation_reconciliation_failed",
                "Gateway mutation state could not be reconciled",
            )
        session = self._sessions.get_visible(client=client, session_id=params.session_id)
        principal = self._sessions.principal_for_client(client=client, session=session)
        run_id = _client_run_id(
            client_id=client.client_id,
            session_id=params.session_id,
            idempotency_key=_require_idempotency(request),
        )
        run = self._state_store.get_run(run_id)
        if run is None:
            return GatewayMutationReconciliation("domain_not_started")
        if (
            run.session_id != params.session_id
            or run.input_fingerprint != _chat_input_fingerprint(params, principal=principal)
        ):
            raise GatewayDispatchError(
                "mutation_reconciliation_failed",
                "Gateway mutation state could not be reconciled",
            )
        return GatewayMutationReconciliation(
            "domain_started",
            serialize_method_result(
                request.method,
                ChatSendResult(session_id=params.session_id, run_id=run_id),
            ),
        )

    async def _dispatch_typed(
        self,
        request: RequestFrame,
        *,
        params: object,
        client: GatewayClientContext,
    ) -> GatewayMethodResult:
        if isinstance(params, HealthParams):
            cursor = self._event_cursor()
            return HealthResult(
                ready=self._safe_ready(),
                server_generation=self._generation,
                event_cursor=cursor,
            )
        if isinstance(params, StatusParams):
            page = self._all_visible_sessions(client)
            session_ids = {row.session_id for row in page}
            active = self._state_store.list_active_runs(limit=1000)
            return StatusResult(
                ready=self._safe_ready(),
                server_generation=self._generation,
                event_cursor=self._event_cursor(),
                active_runs=sum(run.session_id in session_ids for run in active),
                session_count=len(page),
            )
        if isinstance(params, ChannelsListParams):
            return ChannelsListResult(self._channel_snapshots(client))
        if isinstance(params, EventsReplayParams):
            return self._replay(params=params, client=client)
        if isinstance(params, SessionsCreateParams):
            session_id = params.session_id or _generated_session_id(
                client_id=client.client_id,
                idempotency_key=_require_idempotency(request),
            )
            record = self._sessions.create_for_client(
                client=client,
                session_id=session_id,
                mode=params.mode,
                debug=params.debug,
            )
            self._events.emit(
                "session.updated",
                SessionUpdatedEvent(self._sessions.snapshot(record)),
                session_id=record.session_id,
            )
            current = self._state_store.get_session(record.session_id)
            if current is None:
                raise GatewayStateError("created Gateway session is unavailable")
            return SessionsCreateResult(self._sessions.snapshot(current))
        if isinstance(params, SessionsListParams):
            page = self._sessions.list_visible(
                client=client,
                cursor=params.cursor,
                limit=params.limit,
            )
            return SessionsListResult(
                sessions=tuple(self._sessions.snapshot(row) for row in page.sessions),
                next_cursor=page.next_cursor,
            )
        if isinstance(params, SessionsGetParams):
            record = self._sessions.get_visible(client=client, session_id=params.session_id)
            return SessionsGetResult(self._sessions.snapshot(record))
        if isinstance(params, SessionsPatchParams):
            record = self._sessions.patch_visible(
                client=client,
                session_id=params.session_id,
                mode=params.mode,
                debug=params.debug,
            )
            self._events.emit(
                "session.updated",
                SessionUpdatedEvent(self._sessions.snapshot(record)),
                session_id=record.session_id,
            )
            current = self._state_store.get_session(record.session_id)
            if current is None:
                raise GatewayStateError("patched Gateway session is unavailable")
            return SessionsPatchResult(self._sessions.snapshot(current))
        if isinstance(params, ChatSendParams):
            return await self._coordinator.submit_client_turn(
                client=client,
                session_id=params.session_id,
                segments=tuple(params.segments),
                message_id=params.message_id,
                request_id=request.request_id,
                idempotency_key=_require_idempotency(request),
            )
        if isinstance(params, ChatAbortParams):
            return await self._coordinator.abort(
                client=client,
                session_id=params.session_id,
                run_id=params.run_id,
            )
        if isinstance(params, RunsGetParams):
            self._sessions.get_visible(client=client, session_id=params.session_id)
            run = self._state_store.get_run(params.run_id)
            if run is None or run.session_id != params.session_id:
                raise GatewayDispatchError("run_not_found", "Gateway run does not exist")
            return RunsGetResult(_run_snapshot(run))
        if isinstance(params, RunsLatestParams):
            self._sessions.get_visible(client=client, session_id=params.session_id)
            run = self._state_store.latest_run_for_session(params.session_id)
            return RunsLatestResult(None if run is None else _run_snapshot(run))
        if isinstance(params, DeliveriesGetParams):
            self._sessions.get_visible(client=client, session_id=params.session_id)
            records = self._state_store.find_outbound_deliveries(
                session_id=params.session_id,
                run_id=params.run_id,
                outbound_id=params.outbound_id,
            )
            if not records:
                raise GatewayDispatchError(
                    "delivery_not_found",
                    "Gateway delivery does not exist",
                )
            return DeliveriesGetResult(
                tuple(self._delivery_snapshot(record) for record in records)
            )
        if isinstance(params, ApprovalsListParams):
            if params.session_id is None:
                raise GatewayDispatchError(
                    "approval_session_required",
                    "Approval listing requires one visible Gateway session",
                )
            session = self._sessions.get_visible(
                client=client,
                session_id=params.session_id,
            )
            principal = self._sessions.principal_for_client(client=client, session=session)
            approvals, next_cursor = self._approval_service.list(
                actor_ref=principal.actor_ref,
                conversation_ref=conversation_authority_ref(principal),
                session_id=session.session_id,
                cursor=params.cursor,
                limit=params.limit,
            )
            return ApprovalsListResult(approvals=approvals, next_cursor=next_cursor)
        if isinstance(params, ApprovalsResolveParams):
            principal, _session_id = self._approval_principal(
                client=client,
                approval_id=params.approval_id,
            )
            receipt = self._approval_service.resolve_bound(
                approval_id=params.approval_id,
                actor_ref=principal.actor_ref,
                conversation_ref=conversation_authority_ref(principal),
                decision=params.decision,
                challenge=params.challenge,
            )
            return ApprovalsResolveResult(
                approval_id=receipt.approval_id,
                resolved=receipt.resolved,
                accepted=receipt.accepted,
                code=receipt.code,
            )
        raise GatewayDispatchError("unknown_method", "Gateway method is not recognized")

    def _replay(
        self,
        *,
        params: EventsReplayParams,
        client: GatewayClientContext,
    ) -> EventsReplayResult:
        replay = self._state_store.replay_events(params.after_seq, limit=params.limit)
        if replay.resync_required:
            return EventsReplayResult(
                events=(),
                next_cursor=replay.current_cursor,
                current_cursor=replay.current_cursor,
                resync_required=True,
            )
        allowed = frozenset(events_for_scopes(client.scopes))
        items: list[GatewayReplayItem] = []
        for record in replay.events:
            payload = parse_event_payload(record.event, record.payload)
            frame = EventFrame(record.event, record.seq, record.payload)
            if record.event in allowed and self._event_visibility.can_view(
                client=client,
                event=frame,
            ):
                items.append(
                    GatewayReplayItem(
                        event=record.event,
                        seq=record.seq,
                        payload=payload,
                    )
                )
        next_cursor = replay.events[-1].seq if replay.events else params.after_seq
        return EventsReplayResult(
            events=tuple(items),
            next_cursor=next_cursor,
            current_cursor=replay.current_cursor,
            resync_required=False,
        )

    def _channel_snapshots(self, client: GatewayClientContext) -> tuple[ChannelSnapshot, ...]:
        if not is_gateway_admin(client):
            return (
                ChannelSnapshot(
                    account=client_account(client),
                    state="connected",
                    capabilities=("chat.send", "chat.abort"),
                ),
            )
        health = self._channel_runtime.health()
        return tuple(_channel_snapshot(item) for item in health.channels)

    def _approval_principal(
        self,
        *,
        client: GatewayClientContext,
        approval_id: str,
    ) -> tuple[Principal, str]:
        for session in self._all_visible_sessions(client):
            try:
                principal = self._sessions.principal_for_client(
                    client=client,
                    session=session,
                )
            except GatewayDispatchError:
                continue
            snapshot = self._approval_service.get(
                approval_id,
                actor_ref=principal.actor_ref,
                conversation_ref=conversation_authority_ref(principal),
                session_id=session.session_id,
            )
            if snapshot is not None:
                return principal, session.session_id
        raise GatewayDispatchError("approval_not_found", "Gateway approval does not exist")

    def _all_visible_sessions(self, client: GatewayClientContext):
        records: list[SessionRecord] = []
        cursor = 0
        while True:
            page = self._sessions.list_visible(client=client, cursor=cursor, limit=100)
            records.extend(page.sessions)
            if page.next_cursor is None:
                return tuple(records)
            cursor = page.next_cursor

    def _event_cursor(self) -> int:
        return self._state_store.replay_events(0, limit=1).current_cursor

    def _delivery_snapshot(self, record: OutboundRecord) -> DeliverySnapshot:
        session_id = record.envelope.get("session_id")
        run_id = record.envelope.get("run_id")
        if not isinstance(session_id, str) or not isinstance(run_id, str):
            raise GatewayStateError("Gateway delivery binding is corrupt")
        receipts = self._state_store.delivery_receipts(record.outbound_id)
        if not receipts:
            raise GatewayStateError("Gateway delivery evidence is missing")
        return DeliverySnapshot(
            outbound_id=record.outbound_id,
            session_id=session_id,
            run_id=run_id,
            state=cast(Any, record.state),
            receipts=tuple(
                DeliveryReceiptSnapshot(
                    receipt_id=receipt.receipt_id,
                    stage=receipt.stage,
                    observed_at_ms=int(receipt.observed_at * 1000),
                    provider_message_id=receipt.provider_message_id,
                    error_code=receipt.error_code,
                )
                for receipt in receipts
            ),
            provider_message_id=record.provider_message_id,
            error_code=record.error_code,
        )

    def _safe_ready(self) -> bool:
        try:
            return bool(self._ready()) and (
                self._state_store.current_writer_generation() == self._generation
            )
        except Exception:
            return False

    def _default_ready(self) -> bool:
        return self._channel_runtime.health().state == "ready"


def _require_idempotency(request: RequestFrame) -> str:
    key = request.idempotency_key
    if key is None:
        raise GatewayDispatchError(
            "idempotency_key_required",
            "Gateway mutation requires an idempotency key",
        )
    return key


def _generated_session_id(*, client_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        (client_id + "\0" + idempotency_key).encode("utf-8")
    ).hexdigest()[:32]
    return "session_" + digest


def _client_run_id(*, client_id: str, session_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        (client_id + "\0" + session_id + "\0" + idempotency_key).encode("utf-8")
    ).hexdigest()[:32]
    return "run_" + digest


def _chat_input_fingerprint(params: ChatSendParams, *, principal: Principal) -> str:
    if any(not isinstance(segment, TextRpcSegment) for segment in params.segments):
        raise GatewayDispatchError(
            "mutation_reconciliation_failed",
            "Gateway mutation state could not be reconciled",
        )
    canonical_text = "".join(
        segment.text for segment in params.segments if isinstance(segment, TextRpcSegment)
    )
    if not canonical_text.strip():
        raise GatewayDispatchError(
            "mutation_reconciliation_failed",
            "Gateway mutation state could not be reconciled",
        )
    return hashlib.sha256(
        (
            principal.actor_ref
            + "\0"
            + canonical_text
            + "\0"
            + str(params.message_id or "")
        ).encode("utf-8")
    ).hexdigest()


def _run_snapshot(run: RunRecord) -> RunSnapshot:
    state = run.state
    if state not in {
        "accepted",
        "running",
        "abort_requested",
        "recovery_required",
        "completed",
        "aborted",
        "failed",
    }:
        raise GatewayStateError("Gateway run state is invalid")
    segments: tuple[TextRpcSegment, ...] = ()
    error_code: str | None = None
    if state == "completed":
        result = run.result
        final_text = result.get("final_text") if isinstance(result, Mapping) else None
        if not isinstance(final_text, str):
            raise GatewayStateError("completed Gateway run has no final text")
        segments = (TextRpcSegment(final_text),) if final_text else ()
    elif state == "failed":
        error_code = run.error_code
        if not isinstance(error_code, str) or not error_code:
            raise GatewayStateError("failed Gateway run has no error code")
    return RunSnapshot(
        session_id=run.session_id,
        run_id=run.run_id,
        state=cast(ChatRunState, state),
        segments=segments,
        error_code=error_code,
    )


def _channel_snapshot(health: object) -> ChannelSnapshot:
    state = getattr(health, "state", "error")
    projected = {
        "stopped": "disconnected",
        "connecting": "starting",
        "ready": "connected",
        "error": "failed",
    }.get(state, "failed")
    return ChannelSnapshot(
        account=getattr(health, "account"),
        state=cast(ChannelConnectionState, projected),
        capabilities=("message.receive", "message.send") if projected == "connected" else (),
        error_code=getattr(health, "detail_code", None),
    )


__all__ = ["GatewayApplicationDispatcher"]
