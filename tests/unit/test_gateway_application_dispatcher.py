from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import replace
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from chatcopilot.application.sessions import SessionManager
from chatcopilot.application.turns import prepare_channel_turn, prepare_client_turn
from chatcopilot.contracts.turns import ExchangeRef, TurnOutcome
from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
from chatcopilot.channels.base import ChannelDefinitelyNotSubmittedError, ChannelDeliveryUnknownError, ChannelHealth
from chatcopilot.contracts.agent import AgentResult, TurnError
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    DeliveryReceipt,
    MessageSegment,
    OutboundEnvelope,
    ResourceTicket,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.gateway_protocol import RequestFrame
from chatcopilot.contracts.gateway_rpc import ChatUpdateEvent
from chatcopilot.contracts.identity import Role
from chatcopilot.gateway import (
    GatewayApplicationDispatcher,
    GatewayApprovalService,
    GatewayClientContext,
    GatewayCredentialBinding,
    GatewayDispatchError,
    GatewayEventPublisher,
    GatewaySessionEventVisibility,
    GatewaySessionService,
    GatewayServerConfig,
    GatewayStateStore,
    GatewayTurnCoordinator,
    GatewayWebSocketServer,
    StaticGatewayCredentialAuthority,
)
from chatcopilot.gateway.channels import ChannelRuntimeManager
from chatcopilot.gateway.protocol import request_fingerprint
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway.observation_queries import detail as observation_detail


def _async_test(function):
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    run.__name__ = function.__name__
    run.__signature__ = inspect.signature(function)
    return run


def _client(client_id: str, *, admin: bool = False) -> GatewayClientContext:
    scopes = (
        "gateway.read",
        "chat.write",
        "chat.abort",
        "approvals.respond",
    )
    if admin:
        scopes += ("gateway.admin",)
    return GatewayClientContext(
        client_id=client_id,
        client_version="test",
        client_mode="acp",
        protocol=1,
        scopes=scopes,
        capabilities=(),
    )


class _PreparedExecutor:
    prepare_client = staticmethod(prepare_client_turn)

    async def prepare_channel(self, **kwargs):
        return await prepare_channel_turn(workspace_root=Path("unused-workspace"),
                                          resource_materializer=None, **kwargs)

    def close(self):
        pass


class _ImmediateExecutor(_PreparedExecutor):
    def __init__(self, result: AgentResult | None = None) -> None:
        self.requests = []
        self.commits = []
        self.discards = []
        self.result = result or AgentResult("answer", "end_turn")

    async def execute(self, request, *, on_event, cancellation=None):
        self.requests.append(request)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if self.result.stop_reason == "llm_error":
            on_event(TurnError(code="fixture_backend_error", message="fixture diagnostic"))
        else:
            on_event(SimpleNamespace())
        return TurnOutcome(self.result, ExchangeRef())

    def commit_exchange(self, request, outcome, *, envelope, receipt):
        del envelope, receipt
        self.commits.append((request, outcome))
        return outcome

    def discard_exchange(self, request, outcome):
        self.discards.append((request, outcome))


class _BlockingExecutor(_PreparedExecutor):
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, request, *, on_event, cancellation=None):
        del request, on_event
        self.started.set()
        while True:
            await asyncio.sleep(0)
            if cancellation is not None and getattr(cancellation, "is_cancelled", False):
                return TurnOutcome(AgentResult("", "cancelled"))

    def commit_exchange(self, request, outcome, *, envelope, receipt):
        del request, envelope, receipt
        return outcome

    def discard_exchange(self, request, outcome):
        del request, outcome


class _ThreadedCancellationExecutor(_PreparedExecutor):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    async def execute(self, request, *, on_event, cancellation=None):
        del request, on_event

        def run() -> SimpleNamespace:
            self.started.set()
            while cancellation is None or not cancellation.is_cancelled:
                time.sleep(0.001)
            self.stopped.set()
            return TurnOutcome(AgentResult("", "cancelled"))

        return await asyncio.to_thread(run)

    def commit_exchange(self, request, outcome, *, envelope, receipt):
        del request, envelope, receipt
        return outcome

    def discard_exchange(self, request, outcome):
        del request, outcome


class _Driver:
    channel_id = "qq-main"

    def __init__(self) -> None:
        self.account = ChannelAccountRef("qq", "10001")
        self.state = "stopped"
        self.sent = []

    async def start(self) -> None:
        self.state = "ready"

    async def stop(self) -> None:
        self.state = "stopped"

    def health(self) -> ChannelHealth:
        return ChannelHealth(
            channel_id=self.channel_id,
            account=self.account,
            state=self.state,
            connection_generation="connection-1" if self.state == "ready" else None,
        )

    async def send(self, envelope):
        self.sent.append(envelope)
        return DeliveryReceipt(
            receipt_id="provider-receipt",
            outbound_id=envelope.outbound_id,
            stage="provider_acknowledged",
            observed_at=20.0,
            provider_message_id="message-2",
        )


class _FailingDriver(_Driver):
    async def send(self, envelope):
        del envelope
        raise ChannelDefinitelyNotSubmittedError(
            "provider_rejected",
            "Provider rejected the outbound message",
        )


class _UnknownDeliveryDriver(_Driver):
    async def send(self, envelope):
        raise ChannelDeliveryUnknownError("provider_timeout", "Provider acknowledgement timed out")


class _InvalidReceiptDriver(_Driver):
    async def send(self, envelope):
        receipt = await super().send(envelope)
        return replace(receipt, outbound_id='another-outbound')


@pytest.mark.parametrize("driver_type, status", [
    (_Driver, "provider_acknowledged"), (_FailingDriver, "failed"), (_UnknownDeliveryDriver, "delivery_unknown"),
    (_InvalidReceiptDriver, "delivery_unknown"),
])
@_async_test
async def test_observed_delivery_closes_the_same_call_and_preserves_actual_receipts(tmp_path, driver_type, status):
    state, generation, _, _, coordinator, channels, _, _, actor = _runtime(tmp_path)
    coordinator._clock = time.time
    recorder = ObservationRecorder(state, generation)
    driver = driver_type()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    try:
        if driver_type is _Driver:
            await channels.handle_inbound(_event(event_id="observed-delivery", sender="20002"))
        else:
            with pytest.raises((ChannelDefinitelyNotSubmittedError, ChannelDeliveryUnknownError)):
                await channels.handle_inbound(_event(event_id="observed-delivery", sender="20002"))
        request = actor.requests[0]
        run_id = request.metadata['trace_id']
        record = observation_detail(recorder.store, run_id)
        events = record['observations']
        assert request.metadata['parent_span_id'] == 'host:actor'
        assert not any(event['kind'] in {'actor_execution', 'actor_returned', 'response_dispatch', 'channel_returned'} for event in events)
        calls = [event for event in events if event['data'].get('operation') == 'channel.deliver']
        assert len(calls) == 2
        start, finish = calls
        assert start['trace_id'] == finish['trace_id'] == run_id
        assert start['span_id'] == finish['span_id']
        assert (start['phase'], finish['phase']) == ('start', 'finish')
        assert finish['status'] == status
        assert finish['elapsed_ms'] >= 0
        outbound = start['data']['outbound_id']
        assert finish['data']['outbound_id'] == outbound
        assert record['outbox'][0]['outbound_id'] == outbound
        assert record['outbox'][0]['state'] == status
        assert record['receipts'][-1]['stage'] == status
        returned = next(event for event in events if event['data'].get('operation') == 'application.result' and event['phase'] == 'finish')
        assert recorder.store.body(run_id, returned['body_ref'])['payload']['output']['final_text'] == 'answer'
        if driver_type is _InvalidReceiptDriver:
            assert not any(event['status'] == 'provider_acknowledged' for event in calls)
    finally:
        await channels.stop()
        recorder.close()


@_async_test
async def test_admitted_resource_failure_keeps_input_and_failed_preparation(tmp_path):
    from chatcopilot.application.resources import ResourceMaterializationError
    state, generation, _, _, coordinator, channels, _, _, actor = _runtime(tmp_path)
    coordinator._clock = time.time
    recorder = ObservationRecorder(state, generation)
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    event = _event(event_id='resource-failure', sender='20002')
    evidence = event.evidence
    ticket = ResourceTicket('ticket-failure', evidence.account, evidence.conversation,
        evidence.sender.sender_id, evidence.event_id, evidence.message_id, 'image',
        provider_ref={'url': 'https://provider.example/private-input'})
    event = replace(event, resource_tickets=(ticket,))
    try:
        with pytest.raises(ResourceMaterializationError):
            await channels.handle_inbound(event)
        from chatcopilot.gateway.coordinator import _channel_run_id
        run = state.get_run(_channel_run_id(event))
        record = observation_detail(recorder.store, run.run_id)
        assert record['run']['state'] == 'failed'
        assert recorder.store.body(run.run_id, record['run']['input_ref'])['payload']['text']
        rows = record['observations']
        preparation = [row for row in rows if row['data'].get('operation') == 'application.prepare']
        assert [row['phase'] for row in preparation] == ['start', 'finish']
        assert preparation[-1]['status'] == 'failed'
        assert not any(row['data'].get('operation') == 'agent.execute' for row in rows)
        assert actor.requests == []
        for row in rows:
            if row['body_ref']:
                assert 'https://provider.example/private-input' not in str(recorder.store.body(run.run_id, row['body_ref']))
    finally:
        await channels.stop()
        recorder.close()


@_async_test
async def test_mismatched_native_projection_is_omitted_without_rejecting_valid_event(tmp_path):
    from chatcopilot.contracts.gateway import ChannelInputObservation, ChannelInputSegment
    state, generation, _, _, coordinator, channels, _, _, actor = _runtime(tmp_path)
    coordinator._clock = time.time
    recorder = ObservationRecorder(state, generation)
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    event = _event(event_id='wrong-native-projection', sender='20002')
    event = replace(event, input_observation=ChannelInputObservation('onebot', 'group', 1.0,
        'b' * 64, 40, (ChannelInputSegment('text', text='another-event-private-body'),)))
    try:
        await channels.handle_inbound(event)
        run_id = actor.requests[0].run_id
        record = observation_detail(recorder.store, run_id)
        assert record['run']['state'] == 'completed'
        received = next(row for row in record['observations'] if row['data'].get('operation') == 'channel.receive' and row['phase'] == 'start')
        assert received['body_state'] == 'capture_failed'
        body = recorder.store.body(run_id, received['body_ref'])
        assert 'another-event-private-body' not in str(body)
        assert body['payload']['input']['omitted'] == ['input_event_binding_mismatch']
    finally:
        await channels.stop()
        recorder.close()


def _runtime(
    tmp_path: Path,
    *,
    executor=None,
    live_sink=None,
    admission_sink=None,
    qq_users="*",
    ingress_retention_limit: int = 10_000,
    state_store: GatewayStateStore | None = None,
    writer_generation: int | None = None,
):
    state = state_store or GatewayStateStore(tmp_path / "state")
    generation = writer_generation or state.acquire_writer_generation(now=1.0)
    manager = SessionManager(writer_generation=generation)
    sessions = GatewaySessionService(
        state_store=state,
        session_manager=manager,
        generation=generation,
        client_roles={"client-a": Role.OWNER, "client-b": Role.USER},
    )
    events = GatewayEventPublisher(
        state_store=state,
        sessions=sessions,
        generation=generation,
        live_sink=live_sink,
    )
    actor = executor or _ImmediateExecutor()
    coordinator = GatewayTurnCoordinator(
        state_store=state,
        sessions=sessions,
        events=events,
        actor_executor=actor,
        identity_policy=IdentityPolicy(),
        admission_policy=AdmissionPolicy.from_raw(
            qq_users=qq_users,
            policy_version="policy-v1",
        ),
        generation=generation,
        on_admission_decision=admission_sink,
        clock=lambda: 10.0,
    )
    channels = ChannelRuntimeManager(
        state_store=state,
        gateway_ingress=coordinator,
        event_sink=events,
        writer_generation=generation,
        ingress_retention_limit=ingress_retention_limit,
        clock=lambda: 20.0,
    )
    coordinator.set_channel_runtime(channels)
    visibility = GatewaySessionEventVisibility(sessions)
    dispatcher = GatewayApplicationDispatcher(
        state_store=state,
        sessions=sessions,
        events=events,
        coordinator=coordinator,
        channel_runtime=channels,
        approval_service=GatewayApprovalService(state, generation=generation),
        event_visibility=visibility,
        generation=generation,
        ready=lambda: True,
    )
    return state, generation, sessions, events, coordinator, channels, visibility, dispatcher, actor


def _server_for_dispatcher(
    *,
    state: GatewayStateStore,
    generation: int,
    dispatcher: GatewayApplicationDispatcher,
    visibility: GatewaySessionEventVisibility,
) -> GatewayWebSocketServer:
    return GatewayWebSocketServer(
        config=GatewayServerConfig(),
        dispatcher=dispatcher,
        credential_authority=StaticGatewayCredentialAuthority(
            (
                GatewayCredentialBinding(
                    token="r" * 32,
                    client_id="client-a",
                    client_mode="acp",
                    scopes=("gateway.read", "chat.write", "chat.abort"),
                ),
            )
        ),
        event_visibility_policy=visibility,
        state_store=state,
        server_generation=generation,
    )


async def _create(dispatcher, client, key: str):
    return await dispatcher.dispatch(
        RequestFrame(
            request_id="request-" + key,
            method="sessions.create",
            params={},
            idempotency_key=key,
        ),
        client=client,
    )


@_async_test
async def test_client_sessions_are_server_bound_and_admin_is_explicit(tmp_path: Path) -> None:
    *_, dispatcher, _actor = _runtime(tmp_path)
    created_a = await _create(dispatcher, _client("client-a"), "create-a")
    created_b = await _create(dispatcher, _client("client-b"), "create-b")
    session_a = created_a["session"]["sessionId"]

    listed_b = await dispatcher.dispatch(
        RequestFrame("list-b", "sessions.list", {}),
        client=_client("client-b"),
    )
    assert [row["sessionId"] for row in listed_b["sessions"]] == [
        created_b["session"]["sessionId"]
    ]
    with pytest.raises(GatewayDispatchError) as hidden:
        await dispatcher.dispatch(
            RequestFrame("get-a-as-b", "sessions.get", {"sessionId": session_a}),
            client=_client("client-b"),
        )
    assert hidden.value.code == "session_not_found"

    listed_admin = await dispatcher.dispatch(
        RequestFrame("list-admin", "sessions.list", {}),
        client=_client("admin", admin=True),
    )
    assert len(listed_admin["sessions"]) == 2


@_async_test
async def test_event_is_durable_before_live_and_replay_is_visibility_filtered(
    tmp_path: Path,
) -> None:
    observed = []
    holder = {}

    def live(frame):
        state = holder["state"]
        assert state.events_after(frame.seq - 1, limit=1)[0].seq == frame.seq
        observed.append(frame)

    state, _, sessions, events, _, _, visibility, dispatcher, _ = _runtime(
        tmp_path,
        live_sink=live,
    )
    holder["state"] = state
    created_a = await _create(dispatcher, _client("client-a"), "create-a")
    await _create(dispatcher, _client("client-b"), "create-b")
    session_a = created_a["session"]["sessionId"]
    events.emit(
        "chat.update",
        ChatUpdateEvent(session_a, "run-a", "private-a"),
        session_id=session_a,
    )

    replay_b = await dispatcher.dispatch(
        RequestFrame("replay-b", "events.replay", {"afterSeq": 0, "limit": 100}),
        client=_client("client-b"),
    )
    assert all(
        item["payload"].get("sessionId") != session_a
        for item in replay_b["events"]
    )
    assert replay_b["nextCursor"] == replay_b["currentCursor"]
    assert observed
    assert not visibility.can_view(
        client=_client("client-b"),
        event=observed[-1],
    )


@_async_test
async def test_chat_send_is_durable_detached_and_abort_cancels_real_worker(
    tmp_path: Path,
) -> None:
    executor = _BlockingExecutor()
    state, _, _, _, coordinator, _, _, dispatcher, _ = _runtime(
        tmp_path,
        executor=executor,
    )
    created = await _create(dispatcher, _client("client-a"), "create-a")
    session_id = created["session"]["sessionId"]
    accepted = await dispatcher.dispatch(
        RequestFrame(
            "send-a",
            "chat.send",
            {"sessionId": session_id, "segments": [{"kind": "text", "text": "hello"}]},
            idempotency_key="send-key",
        ),
        client=_client("client-a"),
    )
    run_id = accepted["runId"]
    assert state.get_run(run_id) is not None
    await asyncio.wait_for(executor.started.wait(), timeout=1)

    aborted = await dispatcher.dispatch(
        RequestFrame(
            "abort-a",
            "chat.abort",
            {"sessionId": session_id, "runId": run_id},
            idempotency_key="abort-key",
        ),
        client=_client("client-a"),
    )
    assert aborted["aborted"] is True
    for _ in range(100):
        if state.get_run(run_id).state == "aborted" and coordinator.active_task_count == 0:
            break
        await asyncio.sleep(0)
    assert state.get_run(run_id).state == "aborted"
    assert coordinator.active_task_count == 0


@_async_test
async def test_chat_send_reserve_before_domain_restart_retries_only_original_run(
    tmp_path: Path,
) -> None:
    state, first, _, _, old_coordinator, _, _, old_dispatcher, _ = _runtime(tmp_path)
    client = _client("client-a")
    created = await _create(old_dispatcher, client, "create-a")
    session_id = created["session"]["sessionId"]
    request = RequestFrame(
        "send-recovered",
        "chat.send",
        {"sessionId": session_id, "segments": [{"kind": "text", "text": "hello"}]},
        idempotency_key="reserve-before-domain",
    )
    state.reserve_idempotency(
        generation=first,
        client_id=client.client_id,
        method=request.method,
        key=request.idempotency_key or "",
        request_fingerprint=request_fingerprint(request),
    )
    await old_coordinator.close()
    current = state.acquire_writer_generation(now=2.0)
    (
        _,
        _,
        _,
        _,
        coordinator,
        _,
        visibility,
        dispatcher,
        actor,
    ) = _runtime(
        tmp_path,
        state_store=state,
        writer_generation=current,
    )
    server = _server_for_dispatcher(
        state=state,
        generation=current,
        dispatcher=dispatcher,
        visibility=visibility,
    )
    try:
        recovered = await server._execute_request(request, client)
        replayed = await server._execute_request(
            RequestFrame(
                "send-replayed",
                request.method,
                request.params,
                request.idempotency_key,
            ),
            client,
        )
        assert recovered.ok and replayed.ok
        assert recovered.result == replayed.result
        for _ in range(100):
            if len(actor.requests) == 1:
                break
            await asyncio.sleep(0)
        assert len(actor.requests) == 1
    finally:
        await coordinator.close()


@_async_test
async def test_chat_send_domain_before_idempotency_completion_rebuilds_same_receipt(
    tmp_path: Path,
) -> None:
    state, generation, _, _, coordinator, _, visibility, dispatcher, actor = _runtime(tmp_path)
    client = _client("client-a")
    created = await _create(dispatcher, client, "create-a")
    session_id = created["session"]["sessionId"]
    request = RequestFrame(
        "send-reconcile",
        "chat.send",
        {"sessionId": session_id, "segments": [{"kind": "text", "text": "hello"}]},
        idempotency_key="domain-before-completion",
    )
    state.reserve_idempotency(
        generation=generation,
        client_id=client.client_id,
        method=request.method,
        key=request.idempotency_key or "",
        request_fingerprint=request_fingerprint(request),
    )
    accepted = await dispatcher.dispatch(request, client=client)
    server = _server_for_dispatcher(
        state=state,
        generation=generation,
        dispatcher=dispatcher,
        visibility=visibility,
    )
    try:
        recovered = await server._execute_request(request, client)
        replayed = await server._execute_request(
            RequestFrame(
                "send-replayed",
                request.method,
                request.params,
                request.idempotency_key,
            ),
            client,
        )
        assert recovered.ok and replayed.ok
        assert recovered.result == accepted == replayed.result
        for _ in range(100):
            if len(actor.requests) == 1:
                break
            await asyncio.sleep(0)
        assert len(actor.requests) == 1
    finally:
        await coordinator.close()


@_async_test
async def test_runs_get_returns_visible_durable_terminal_output(tmp_path: Path) -> None:
    state, _, _, _, coordinator, _, _, dispatcher, _ = _runtime(tmp_path)
    owner = _client("client-a")
    created = await _create(dispatcher, owner, "create-a")
    session_id = created["session"]["sessionId"]
    accepted = await dispatcher.dispatch(
        RequestFrame(
            "send-a",
            "chat.send",
            {"sessionId": session_id, "segments": [{"kind": "text", "text": "hello"}]},
            idempotency_key="send-key",
        ),
        client=owner,
    )
    run_id = accepted["runId"]
    for _ in range(100):
        if state.get_run(run_id).state == "completed":
            break
        await asyncio.sleep(0)

    result = await dispatcher.dispatch(
        RequestFrame(
            "get-run",
            "runs.get",
            {"sessionId": session_id, "runId": run_id},
        ),
        client=owner,
    )
    assert result == {
        "run": {
            "sessionId": session_id,
            "runId": run_id,
            "state": "completed",
            "segments": [{"kind": "text", "text": "answer"}],
        }
    }
    latest = await dispatcher.dispatch(
        RequestFrame(
            "get-latest-run",
            "runs.latest",
            {"sessionId": session_id},
        ),
        client=owner,
    )
    assert latest == result
    with pytest.raises(GatewayDispatchError) as hidden:
        await dispatcher.dispatch(
            RequestFrame(
                "get-hidden-run",
                "runs.latest",
                {"sessionId": session_id},
            ),
            client=_client("client-b"),
        )
    assert hidden.value.code == "session_not_found"
    await coordinator.close()


@_async_test
async def test_deliveries_get_preserves_restart_delivery_unknown_evidence(
    tmp_path: Path,
) -> None:
    state, generation, _, _, old_coordinator, _, _, dispatcher, _ = _runtime(tmp_path)
    owner = _client("client-a")
    created = await _create(dispatcher, owner, "create-delivery")
    session_id = created["session"]["sessionId"]
    run_id = "run-before-delivery-restart"
    state.begin_run(
        generation=generation,
        session_id=session_id,
        run_id=run_id,
        input_fingerprint="d" * 64,
        now=2.0,
    )
    state.start_run(
        generation=generation,
        session_id=session_id,
        run_id=run_id,
        now=3.0,
    )
    envelope = OutboundEnvelope(
        outbound_id="outbound-before-delivery-restart",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "30003"),
        segments=(MessageSegment("text", text="possibly delivered"),),
        created_at=4.0,
        session_id=session_id,
        run_id=run_id,
    )
    state.enqueue_outbound(generation=generation, envelope=envelope)
    state.begin_outbound_submission(
        generation=generation,
        outbound_id=envelope.outbound_id,
        now=5.0,
    )
    state.mark_outbound_submitted(
        generation=generation,
        outbound_id=envelope.outbound_id,
        now=6.0,
    )
    replacement_generation = state.acquire_writer_generation(now=7.0)
    state.resolve_run_recovery(
        generation=replacement_generation,
        session_id=session_id,
        run_id=run_id,
        outcome="failed",
        error_code="gateway_restart_recovery",
        now=8.0,
    )
    await old_coordinator.close()
    _, _, _, _, coordinator, _, _, recovered, _ = _runtime(
        tmp_path,
        state_store=state,
        writer_generation=replacement_generation,
    )
    try:
        by_run = await recovered.dispatch(
            RequestFrame(
                "delivery-by-run",
                "deliveries.get",
                {"sessionId": session_id, "runId": run_id},
            ),
            client=owner,
        )
        assert by_run == {
            "deliveries": [
                {
                    "outboundId": envelope.outbound_id,
                    "sessionId": session_id,
                    "runId": run_id,
                    "state": "delivery_unknown",
                    "errorCode": "gateway_restarted_before_ack",
                    "receipts": [
                        {
                            "receiptId": state.delivery_receipts(envelope.outbound_id)[0].receipt_id,
                            "stage": "gateway_accepted",
                            "observedAtMs": 4_000,
                        },
                        {
                            "receiptId": state.delivery_receipts(envelope.outbound_id)[1].receipt_id,
                            "stage": "provider_submitted",
                            "observedAtMs": 6_000,
                        },
                        {
                            "receiptId": state.delivery_receipts(envelope.outbound_id)[2].receipt_id,
                            "stage": "delivery_unknown",
                            "observedAtMs": 7_000,
                            "errorCode": "gateway_restarted_before_ack",
                        },
                    ],
                }
            ]
        }
        by_outbound = await recovered.dispatch(
            RequestFrame(
                "delivery-by-outbound",
                "deliveries.get",
                {"sessionId": session_id, "outboundId": envelope.outbound_id},
            ),
            client=owner,
        )
        assert by_outbound == by_run
        with pytest.raises(GatewayDispatchError) as hidden:
            await recovered.dispatch(
                RequestFrame(
                    "delivery-hidden",
                    "deliveries.get",
                    {"sessionId": session_id, "runId": run_id},
                ),
                client=_client("client-b"),
            )
        assert hidden.value.code == "session_not_found"
    finally:
        await coordinator.close()


@_async_test
async def test_chat_send_reconciliation_fails_closed_on_domain_fingerprint_drift(
    tmp_path: Path,
) -> None:
    state, generation, _, _, coordinator, _, visibility, dispatcher, actor = _runtime(tmp_path)
    client = _client("client-a")
    created = await _create(dispatcher, client, "create-a")
    session_id = created["session"]["sessionId"]
    key = "drifted-domain"
    request = RequestFrame(
        "send-drifted",
        "chat.send",
        {"sessionId": session_id, "segments": [{"kind": "text", "text": "hello"}]},
        idempotency_key=key,
    )
    run_id = "run_" + hashlib.sha256(
        (client.client_id + "\0" + session_id + "\0" + key).encode("utf-8")
    ).hexdigest()[:32]
    state.reserve_idempotency(
        generation=generation,
        client_id=client.client_id,
        method=request.method,
        key=key,
        request_fingerprint=request_fingerprint(request),
    )
    state.begin_run(
        generation=generation,
        session_id=session_id,
        run_id=run_id,
        input_fingerprint="0" * 64,
    )
    server = _server_for_dispatcher(
        state=state,
        generation=generation,
        dispatcher=dispatcher,
        visibility=visibility,
    )
    response = await server._execute_request(request, client)
    assert response.error is not None
    assert response.error.code == "mutation_reconciliation_failed"
    assert actor.requests == []
    await coordinator.close()


def _event(*, event_id: str, sender: str, group: str = "30003") -> CanonicalInboundEvent:
    return CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=ChannelAccountRef("qq", "10001"),
            conversation=ConversationRef("group", group),
            sender=SenderClaim(sender, "Actor"),
            event_id=event_id,
            message_id=event_id,
            connection_generation="connection-1",
            frame_sha256="a" * 64,
            observed_at=10.0,
        ),
        segments=(MessageSegment("text", text="hello"),),
    )


@_async_test
async def test_qq_group_shares_conversation_but_preserves_two_actor_principals_and_receipts(
    tmp_path: Path,
) -> None:
    state, _, _, _, _, channels, _, _, actor = _runtime(tmp_path, qq_users="")
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    try:
        await channels.handle_inbound(_event(event_id="event-1", sender="20002"))
        await channels.handle_inbound(_event(event_id="event-2", sender="20003"))
    finally:
        await channels.stop()

    assert len(state.list_sessions()) == 1
    assert len(actor.requests) == 2
    assert actor.requests[0].session_id == actor.requests[1].session_id
    assert actor.requests[0].principal.actor_ref != actor.requests[1].principal.actor_ref
    assert all(request.principal.role is Role.USER for request in actor.requests)
    assert len(actor.commits) == 2
    assert actor.discards == []
    assert len(driver.sent) == 2
    for envelope in driver.sent:
        receipts = state.delivery_receipts(envelope.outbound_id)
        assert [receipt.stage for receipt in receipts] == [
            "gateway_accepted",
            "provider_submitted",
            "provider_acknowledged",
        ]


@_async_test
async def test_failed_provider_delivery_is_not_published_to_group_journal(
    tmp_path: Path,
) -> None:
    state, _, _, _, _, channels, _, _, actor = _runtime(tmp_path)
    driver = _FailingDriver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    try:
        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await channels.handle_inbound(
                _event(event_id="event-delivery-failed", sender="20002")
            )
    finally:
        await channels.stop()

    assert len(actor.requests) == 1
    assert actor.commits == []
    assert len(actor.discards) == 1
    run = state.get_run(
        "run_"
        + hashlib.sha256(b"qq\x0010001\x00event-delivery-failed").hexdigest()[:32]
    )
    assert run is not None
    assert run.state == "failed"


@_async_test
async def test_duplicate_admitted_ingress_authorizes_once_and_executes_once(
    tmp_path: Path,
) -> None:
    decisions = []
    state, _, _, _, _, channels, _, _, actor = _runtime(
        tmp_path,
        admission_sink=decisions.append,
    )
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    event = _event(event_id="event-once", sender="20002")
    try:
        await channels.handle_inbound(event)
        await channels.handle_inbound(event)
    finally:
        await channels.stop()

    assert len(decisions) == 1
    assert len(actor.requests) == 1
    record = state.get_ingress(channel="qq", account_id="10001", event_id="event-once")
    assert record is not None
    assert record.state == "completed"


@_async_test
async def test_cancelled_channel_caller_waits_for_real_worker_exit(tmp_path: Path) -> None:
    executor = _ThreadedCancellationExecutor()
    state, _, _, _, coordinator, channels, _, _, _ = _runtime(
        tmp_path,
        executor=executor,
    )
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    ingress = asyncio.create_task(
        channels.handle_inbound(_event(event_id="event-cancel", sender="20002"))
    )
    try:
        assert await asyncio.to_thread(executor.started.wait, 1)
        ingress.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ingress
        assert executor.stopped.is_set()
        run_id = "run_" + hashlib.sha256(
            b"qq\x0010001\x00event-cancel"
        ).hexdigest()[:32]
        assert state.get_run(run_id).state == "aborted"
        assert coordinator.active_task_count == 0
    finally:
        await channels.stop()


@_async_test
async def test_private_admission_rejection_precedes_session_and_agent_side_effects(tmp_path: Path) -> None:
    state = GatewayStateStore(tmp_path / "state")
    generation = state.acquire_writer_generation(now=1.0)
    manager = SessionManager(writer_generation=generation)
    sessions = GatewaySessionService(
        state_store=state,
        session_manager=manager,
        generation=generation,
    )
    events = GatewayEventPublisher(
        state_store=state,
        sessions=sessions,
        generation=generation,
    )
    actor = _ImmediateExecutor()
    decisions = []
    coordinator = GatewayTurnCoordinator(
        state_store=state,
        sessions=sessions,
        events=events,
        actor_executor=actor,
        identity_policy=IdentityPolicy(),
        admission_policy=AdmissionPolicy.from_raw(
            qq_users="",
            policy_version="policy-v1",
        ),
        generation=generation,
        on_admission_decision=decisions.append,
    )
    channels = ChannelRuntimeManager(
        state_store=state,
        gateway_ingress=coordinator,
        writer_generation=generation,
    )
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    try:
        for index in range(64):
            secret = f"TOP_SECRET_DENIED_PAYLOAD_{index}"
            event = _event(event_id=f"denied-{index}", sender="20002")
            evidence = event.evidence
            ticket_id = f"ticket-{index}"
            event = CanonicalInboundEvent(
                evidence=TransportEvidence(
                    account=evidence.account,
                    conversation=ConversationRef("p2p", evidence.sender.sender_id),
                    sender=evidence.sender,
                    event_id=evidence.event_id,
                    message_id=evidence.message_id,
                    connection_generation=evidence.connection_generation,
                    frame_sha256=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                    observed_at=evidence.observed_at,
                ),
                segments=(
                    MessageSegment("text", text=secret),
                    MessageSegment("image", resource_ticket_id=ticket_id),
                ),
                resource_tickets=(
                    ResourceTicket(
                        ticket_id=ticket_id,
                        account=evidence.account,
                        conversation=ConversationRef("p2p", evidence.sender.sender_id),
                        sender_id=evidence.sender.sender_id,
                        event_id=evidence.event_id,
                        message_id=evidence.message_id,
                        kind="image",
                        expires_at=100.0,
                        provider_ref={
                            "url": f"https://forbidden-provider.example/{secret}.png"
                        },
                    ),
                ),
            )
            with pytest.raises(Exception) as rejected:
                await channels.handle_inbound(event)
            assert getattr(rejected.value, "code") == "qq-private-user-not-allowed"
    finally:
        await channels.stop()
    assert len(decisions) == 64
    assert state.list_sessions() == ()
    assert actor.requests == []
    assert state.list_ingress(
        states=("accepted", "processing", "completed", "failed", "recovery_required"),
        limit=1000,
    ) == ()
    database_bytes = state.database_path.read_bytes()
    assert b"TOP_SECRET_DENIED_PAYLOAD" not in database_bytes
    assert b"forbidden-provider.example" not in database_bytes


@_async_test
async def test_stale_generation_fails_closed(tmp_path: Path) -> None:
    state, *_rest, dispatcher, _actor = _runtime(tmp_path)
    state.acquire_writer_generation(now=2.0)
    with pytest.raises(GatewayDispatchError) as stale:
        await dispatcher.dispatch(
            RequestFrame("health", "health", {}),
            client=_client("client-a"),
        )
    assert stale.value.code == "gateway_generation_stale"


@pytest.mark.parametrize("entrypoint", ["channel", "client"])
@pytest.mark.parametrize("stop_reason, final_text", [
    ("llm_error", "Execution failed"), ("llm_error", ""),
    ("end_turn", "answer"), ("cancelled", ""),
    pytest.param("end_turn", "x" * 70_000, id="large-success"),
    pytest.param("llm_error", "x" * 70_000, id="large-error"),
    ("tool_failure_cap", "stopped"), ("iteration_cap", "stopped"),
    ("tool_call_cap", "stopped"), ("timeout_cap", "stopped"),
])
@_async_test
async def test_agent_result_terminal_preserves_execution_and_delivery(
    tmp_path, entrypoint, stop_reason, final_text,
):
    actor = _ImmediateExecutor(AgentResult(final_text, stop_reason))
    state, generation, _, _, coordinator, channels, _, dispatcher, _ = _runtime(
        tmp_path, executor=actor,
    )
    coordinator._clock = time.time
    recorder = ObservationRecorder(state, generation)
    driver = _Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    try:
        if entrypoint == "channel":
            event = _event(event_id="result-outcome", sender="20002")
            await channels.handle_inbound(event)
            await channels.handle_inbound(event)
            run_id = actor.requests[0].run_id
        else:
            client = _client("client-a")
            created = await _create(dispatcher, client, "result-session")
            accepted = await dispatcher.dispatch(
                RequestFrame("send-result", "chat.send", {
                    "sessionId": created["session"]["sessionId"],
                    "segments": [{"kind": "text", "text": "fixture request"}],
                }, idempotency_key="result-request"), client=client,
            )
            run_id = accepted["runId"]
            for _ in range(100):
                if state.get_run(run_id).state in {"completed", "failed", "aborted"}:
                    break
                await asyncio.sleep(0)
        run = state.get_run(run_id)
        expected_state = (
            "failed" if stop_reason == "llm_error" else
            "aborted" if stop_reason == "cancelled" else "completed"
        )
        assert run.state == expected_state
        assert run.error_code == ("agent_llm_error" if stop_reason == "llm_error" else None)
        assert run.result == {"final_text": final_text, "stop_reason": stop_reason}
        assert len(actor.requests) == 1
        assert state.get_session(run.session_id).active_run_id is None
        terminals = [event for event in state.events_after(0, limit=100)
                     if event.event in {"chat.final", "chat.error"}
                     and event.payload["runId"] == run_id]
        assert len(terminals) == 1
        if stop_reason == "llm_error":
            assert terminals[0].event == "chat.error"
            assert terminals[0].payload["code"] == "agent_llm_error"
            assert terminals[0].payload["retryable"] is False
        else:
            assert terminals[0].event == "chat.final"
            assert terminals[0].payload["stopReason"] == (
                "aborted" if stop_reason == "cancelled" else "completed"
            )
        observed = observation_detail(recorder.store, run_id)
        assert observed["run"]["state"] == expected_state
        assert observed["run"]["error_code"] == run.error_code
        if len(final_text) < 64 * 1024:
            assert recorder.store.body(run_id, observed["run"]["result_ref"])["payload"] == run.result
        if stop_reason == "llm_error":
            errors = [event for event in observed["observations"] if event["kind"] == "TurnError"]
            assert [event["error_code"] for event in errors] == ["fixture_backend_error"]
        if entrypoint == "channel" and final_text:
            assert len(driver.sent) == len(actor.commits) == 1
            assert not actor.discards
            assert driver.sent[0].segments[0].text == final_text
            assert [receipt.stage for receipt in state.delivery_receipts(driver.sent[0].outbound_id)] == [
                "gateway_accepted", "provider_submitted", "provider_acknowledged",
            ]
            assert observed["outbox"][0]["state"] == "provider_acknowledged"
            assert observed["receipts"][-1]["stage"] == "provider_acknowledged"
        else:
            assert not driver.sent and not actor.commits
            assert len(actor.discards) == 1
    finally:
        await channels.stop()
        await coordinator.close()
        recorder.close()


@pytest.mark.parametrize("failure", ["delivery", "unknown_delivery", "exchange", "terminal_write", "generation"])
@_async_test
async def test_agent_failure_does_not_override_delivery_or_persistence_errors(tmp_path, monkeypatch, failure):
    from chatcopilot.gateway.state_store import StaleWriterGeneration

    actor = _ImmediateExecutor(AgentResult("Execution failed", "llm_error"))
    state, _, _, _, coordinator, channels, _, _, _ = _runtime(tmp_path, executor=actor)

    class Driver(_Driver):
        async def send(self, envelope):
            receipt = await super().send(envelope)
            if failure == "delivery":
                raise ChannelDefinitelyNotSubmittedError("provider_rejected", "fixture rejection")
            if failure == "unknown_delivery":
                raise ChannelDeliveryUnknownError("provider_timeout", "fixture timeout")
            if failure == "generation":
                state.acquire_writer_generation(now=21.0)
            return receipt

    if failure == "exchange":
        def fail_commit(*args, **kwargs):
            raise OSError("fixture journal failure after acknowledgement")
        monkeypatch.setattr(actor, "commit_exchange", fail_commit)
    if failure == "terminal_write":
        original_finish = state.finish_run

        def fail_result_once(**kwargs):
            if kwargs.get("error_code") == "agent_llm_error":
                raise OSError("fixture terminal persistence failure")
            return original_finish(**kwargs)
        monkeypatch.setattr(state, "finish_run", fail_result_once)
    driver = Driver()
    channels.register(driver)
    await channels.start()
    await channels.activate()
    try:
        with pytest.raises((ChannelDefinitelyNotSubmittedError, ChannelDeliveryUnknownError, OSError, StaleWriterGeneration)):
            await channels.handle_inbound(_event(event_id="failed-result-boundary", sender="20002"))
        run = state.get_run(actor.requests[0].run_id)
        assert run.error_code != "agent_llm_error"
        if failure != "generation":
            assert run.result == {"final_text": "Execution failed", "stop_reason": "llm_error"}
        assert len(driver.sent) == 1
        receipts = state.delivery_receipts(driver.sent[0].outbound_id)
        if failure in {"exchange", "terminal_write"}:
            assert receipts[-1].stage == "provider_acknowledged"
            assert run.state == "failed" and run.error_code == "gateway_turn_failed"
        elif failure == "delivery":
            assert receipts[-1].stage == "failed"
            assert run.error_code == "provider_rejected"
        elif failure == "unknown_delivery":
            assert receipts[-1].stage == "delivery_unknown"
            assert run.error_code == "provider_timeout"
        else:
            assert run.state != "completed"
        if failure == "terminal_write":
            assert len(actor.commits) == 1 and not actor.discards
        else:
            assert not actor.commits and len(actor.discards) == 1
    finally:
        await channels.stop()
        await coordinator.close()
