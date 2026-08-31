from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from chatcopilot.contracts.authorization import AuthorizationDecision, Principal
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    OutboundEnvelope,
    ResourceTicket,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.gateway.state_store import (
    GatewayInstanceLeaseUnavailable,
    GatewayStateError,
    GatewayStateStore,
    IdempotencyConflict,
    IngressConflict,
    RunConflict,
    SessionConflict,
    SessionRecord,
    StaleWriterGeneration,
)


def _inbound(*, event_id: str = "event-1", body: str = "hello") -> CanonicalInboundEvent:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    account = ChannelAccountRef(channel="qq_personal", account_id="10001")
    conversation = ConversationRef(kind="group", conversation_id="20001")
    return CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=account,
            conversation=conversation,
            sender=SenderClaim(sender_id="30001", display_name="sender"),
            event_id=event_id,
            message_id="40001",
            connection_generation="connection-1",
            frame_sha256=digest,
            observed_at=100.0,
        ),
        segments=(
            MessageSegment(kind="text", text=body),
            MessageSegment(kind="image", resource_ticket_id="ticket-1"),
        ),
        resource_tickets=(
            ResourceTicket(
                ticket_id="ticket-1",
                account=account,
                conversation=conversation,
                sender_id="30001",
                event_id=event_id,
                message_id="40001",
                kind="image",
                provider_ref={"file_id": "provider-file-1", "url": "https://example.invalid/a"},
            ),
        ),
    )


def _outbound(*, outbound_id: str = "outbound-1", text: str = "reply") -> OutboundEnvelope:
    return OutboundEnvelope(
        outbound_id=outbound_id,
        account=ChannelAccountRef(channel="qq_personal", account_id="10001"),
        conversation=ConversationRef(kind="group", conversation_id="20001"),
        segments=(MessageSegment(kind="text", text=text),),
        created_at=200.0,
        session_id="session-1",
        run_id="run-1",
    )


def _principal(event: CanonicalInboundEvent) -> Principal:
    evidence = event.evidence
    return Principal(
        channel=evidence.account.channel,
        account_id=evidence.account.account_id,
        conversation=ConversationIdentity(
            platform=evidence.account.channel,
            chat_kind=evidence.conversation.kind,
            chat_id=evidence.conversation.conversation_id,
        ),
        user_id=evidence.sender.sender_id,
        role=Role.USER,
        evidence_digest="sha256:" + evidence.frame_sha256,
    )


def _account(account_id: str = "account-1") -> ChannelAccountRef:
    return ChannelAccountRef(channel="qq_personal", account_id=account_id)


def _conversation(conversation_id: str = "conversation-1") -> ConversationRef:
    return ConversationRef(kind="group", conversation_id=conversation_id)


def _decision(index: int, *, actor_ref: str = "qq:actor-1") -> AuthorizationDecision:
    return AuthorizationDecision(
        decision_id=f"authz-{index}",
        request_id=f"request-{index}",
        request_digest="sha256:" + hashlib.sha256(str(index).encode()).hexdigest(),
        allowed=index % 2 == 0,
        code="allowed" if index % 2 == 0 else "denied",
        policy_version="policy-v1",
        actor_ref=actor_ref,
    )


def test_store_creates_private_sqlite_state(tmp_path: Path) -> None:
    root = tmp_path / "gateway-state"
    store = GatewayStateStore(root)
    assert store.current_writer_generation() == 0
    assert root.is_dir()
    assert store.database_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
        assert store.database_path.stat().st_nlink == 1


@pytest.mark.skipif(os.name != "posix", reason="Gateway runtime is Linux/WSL only")
def test_instance_lease_is_exclusive_across_processes_and_released(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    lease = store.acquire_instance_lease()
    source_root = Path(__file__).resolve().parents[2] / "src"
    environ = os.environ.copy()
    existing_pythonpath = environ.get("PYTHONPATH")
    environ["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(source_root), existing_pythonpath)
        if value
    )
    contender = (
        "from pathlib import Path\n"
        "import sys\n"
        "from chatcopilot.gateway.state_store import "
        "GatewayInstanceLeaseUnavailable, GatewayStateStore\n"
        "store = GatewayStateStore(Path(sys.argv[1]), trusted_anchor=Path(sys.argv[2]))\n"
        "try:\n"
        "    lease = store.acquire_instance_lease()\n"
        "except GatewayInstanceLeaseUnavailable:\n"
        "    raise SystemExit(23)\n"
        "lease.close()\n"
    )

    blocked = subprocess.run(
        [sys.executable, "-c", contender, str(store.root), str(store.trusted_anchor)],
        env=environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert blocked.returncode == 23, blocked.stderr

    lease.close()
    released = subprocess.run(
        [sys.executable, "-c", contender, str(store.root), str(store.trusted_anchor)],
        env=environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert released.returncode == 0, released.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX file metadata contract")
@pytest.mark.parametrize("unsafe_kind", ("mode", "symlink", "hardlink"))
def test_instance_lease_rejects_unsafe_lock_file(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    lease_path = store.root / "gateway.instance.lock"
    target = tmp_path / "lease-target"
    target.write_bytes(b"")
    target.chmod(0o600)
    if unsafe_kind == "mode":
        lease_path.write_bytes(b"")
        lease_path.chmod(0o644)
    elif unsafe_kind == "symlink":
        lease_path.symlink_to(target)
    else:
        os.link(target, lease_path)

    with pytest.raises((GatewayStateError, PermissionError)):
        store.acquire_instance_lease()


def test_instance_lease_contention_uses_specific_error(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    lease = store.acquire_instance_lease()
    try:
        with pytest.raises(GatewayInstanceLeaseUnavailable):
            store.acquire_instance_lease()
    finally:
        lease.close()


def test_writer_generation_fences_stale_mutations(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first = store.acquire_writer_generation(now=1.0)
    store.append_event(generation=first, event="channel.status", payload={"ready": True})
    second = store.acquire_writer_generation(now=2.0)
    assert second == first + 1
    with pytest.raises(StaleWriterGeneration):
        store.append_event(generation=first, event="channel.status", payload={})
    current = store.append_event(
        generation=second,
        event="channel.status",
        payload={"generation": second},
        now=3.0,
    )
    assert current.seq == 2
    assert [record.seq for record in store.events_after(0)] == [1, 2]


def test_authorization_decisions_are_durable_deduplicated_bounded_and_fenced(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first = store.acquire_writer_generation(now=1.0)
    original = _decision(1)
    recorded = store.record_authorization_decision(
        generation=first,
        decision=original,
        retain_last=2,
        now=2.0,
    )
    assert recorded.decision == original
    assert store.record_authorization_decision(
        generation=first,
        decision=original,
        retain_last=2,
        now=3.0,
    ) == recorded

    store.record_authorization_decision(
        generation=first,
        decision=_decision(2),
        retain_last=2,
        now=4.0,
    )
    store.record_authorization_decision(
        generation=first,
        decision=_decision(3, actor_ref="qq:actor-2"),
        retain_last=2,
        now=5.0,
    )
    assert [
        item.decision.decision_id for item in store.list_authorization_decisions()
    ] == ["authz-3", "authz-2"]
    assert store.list_authorization_decisions(actor_ref="qq:actor-1")[0].decision == _decision(2)

    second = store.acquire_writer_generation(now=6.0)
    with pytest.raises(StaleWriterGeneration):
        store.record_authorization_decision(
            generation=first,
            decision=_decision(4),
        )
    assert store.record_authorization_decision(
        generation=second,
        decision=_decision(4),
        now=7.0,
    ).generation == second


def test_idempotency_replays_completed_result_and_rejects_drift(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    fingerprint = "a" * 64
    first = store.reserve_idempotency(
        generation=generation,
        client_id="acp-edge",
        method="chat.send",
        key="idem-1",
        request_fingerprint=fingerprint,
        now=10.0,
    )
    assert first.state == "reserved"
    pending = store.reserve_idempotency(
        generation=generation,
        client_id="acp-edge",
        method="chat.send",
        key="idem-1",
        request_fingerprint=fingerprint,
        now=11.0,
    )
    assert pending.state == "pending"

    with pytest.raises(IdempotencyConflict):
        store.reserve_idempotency(
            generation=generation,
            client_id="acp-edge",
            method="chat.send",
            key="idem-1",
            request_fingerprint="b" * 64,
        )

    response = {"runId": "run-1", "accepted": True}
    store.complete_idempotency(
        generation=generation,
        client_id="acp-edge",
        method="chat.send",
        key="idem-1",
        request_fingerprint=fingerprint,
        response=response,
    )
    replay = store.reserve_idempotency(
        generation=generation,
        client_id="acp-edge",
        method="chat.send",
        key="idem-1",
        request_fingerprint=fingerprint,
        now=12.0,
    )
    assert replay.state == "completed"
    assert replay.response == response


def test_pending_idempotency_requires_explicit_new_generation_recovery(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first = store.acquire_writer_generation()
    store.reserve_idempotency(
        generation=first,
        client_id="client",
        method="chat.send",
        key="idem",
        request_fingerprint="c" * 64,
    )
    second = store.acquire_writer_generation()
    pending = store.reserve_idempotency(
        generation=second,
        client_id="client",
        method="chat.send",
        key="idem",
        request_fingerprint="c" * 64,
    )
    assert pending.state == "recovery_required"
    assert pending.generation == first
    with pytest.raises(StaleWriterGeneration):
        store.complete_idempotency(
            generation=second,
            client_id="client",
            method="chat.send",
            key="idem",
            request_fingerprint="c" * 64,
            response={"accepted": True},
        )
    recovered = store.resolve_idempotency_recovery(
        generation=second,
        client_id="client",
        method="chat.send",
        key="idem",
        request_fingerprint="c" * 64,
        resolution="retry",
    )
    assert recovered.state == "reserved"
    store.complete_idempotency(
        generation=second,
        client_id="client",
        method="chat.send",
        key="idem",
        request_fingerprint="c" * 64,
        response={"accepted": True},
    )


def test_ingress_is_durable_deduplicated_and_generation_owned(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    event = _inbound()
    assert store.reserve_ingress(
        generation=generation, event=event, principal=_principal(event)
    ).state == "reserved"
    assert store.reserve_ingress(
        generation=generation, event=event, principal=_principal(event)
    ).state == "accepted"
    accepted = store.get_ingress(
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
    )
    assert accepted is not None
    assert accepted.event == event
    assert accepted.principal == _principal(event)
    mismatched = _principal(event)
    mismatched = Principal(
        channel=mismatched.channel,
        account_id=mismatched.account_id,
        conversation=mismatched.conversation,
        user_id=mismatched.user_id,
        role=Role.OWNER,
        evidence_digest=mismatched.evidence_digest,
    )
    with pytest.raises(IngressConflict, match="another principal"):
        store.reserve_ingress(
            generation=generation,
            event=event,
            principal=mismatched,
        )
    assert store.claim_ingress(
        generation=generation,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
    )
    assert not store.claim_ingress(
        generation=generation,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
    )
    store.finish_ingress(
        generation=generation,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
        succeeded=True,
    )
    assert store.reserve_ingress(
        generation=generation, event=event, principal=_principal(event)
    ).state == "completed"

    drifted = _inbound(body="different")
    with pytest.raises(IngressConflict):
        store.reserve_ingress(
            generation=generation, event=drifted, principal=_principal(drifted)
        )


def test_terminal_ingress_retention_is_bounded_and_never_deletes_active_rows(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation(now=1.0)
    for index in range(5):
        event = _inbound(event_id=f"terminal-{index}", body=f"body-{index}")
        store.reserve_ingress(
            generation=generation,
            event=event,
            principal=_principal(event),
            now=10.0 + index,
        )
        assert store.claim_ingress(
            generation=generation,
            channel="qq_personal",
            account_id="10001",
            event_id=event.evidence.event_id,
            now=20.0 + index,
        )
        store.finish_ingress(
            generation=generation,
            channel="qq_personal",
            account_id="10001",
            event_id=event.evidence.event_id,
            succeeded=index % 2 == 0,
            retain_terminal=2,
            now=30.0 + index,
        )

    accepted = _inbound(event_id="active-accepted", body="accepted")
    processing = _inbound(event_id="active-processing", body="processing")
    store.reserve_ingress(
        generation=generation,
        event=accepted,
        principal=_principal(accepted),
        now=40.0,
    )
    store.reserve_ingress(
        generation=generation,
        event=processing,
        principal=_principal(processing),
        now=41.0,
    )
    assert store.claim_ingress(
        generation=generation,
        channel="qq_personal",
        account_id="10001",
        event_id="active-processing",
        now=42.0,
    )

    with sqlite3.connect(store.database_path) as connection:
        terminal_count = connection.execute(
            "SELECT COUNT(*) FROM ingress WHERE state IN ('completed', 'failed')"
        ).fetchone()[0]
    assert terminal_count == 2
    assert store.prune_terminal_ingress(generation=generation, retain_last=1) == 1
    assert store.get_ingress(
        channel="qq_personal",
        account_id="10001",
        event_id="active-accepted",
    ).state == "accepted"
    assert store.get_ingress(
        channel="qq_personal",
        account_id="10001",
        event_id="active-processing",
    ).state == "processing"

    store.acquire_writer_generation(now=50.0)
    with pytest.raises(StaleWriterGeneration):
        store.prune_terminal_ingress(generation=generation, retain_last=1)


def test_interrupted_ingress_requires_explicit_recovery(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first = store.acquire_writer_generation()
    event = _inbound()
    store.reserve_ingress(generation=first, event=event, principal=_principal(event))
    assert store.claim_ingress(
        generation=first,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
    )
    second = store.acquire_writer_generation()
    reservation = store.reserve_ingress(
        generation=second, event=event, principal=_principal(event)
    )
    assert reservation.state == "recovery_required"
    records = store.list_ingress(states=("recovery_required",))
    assert len(records) == 1
    assert records[0].payload["resource_tickets"][0]["provider_ref"]["file_id"] == (
        "provider-file-1"
    )
    assert not store.claim_ingress(
        generation=second,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
    )
    recovered = store.resolve_ingress_recovery(
        generation=second,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
        retry=True,
        now=300.0,
    )
    assert recovered.state == "accepted"
    assert recovered.generation == second
    assert store.claim_ingress(
        generation=second,
        channel="qq_personal",
        account_id="10001",
        event_id="event-1",
    )


def test_outbound_receipts_preserve_observed_delivery_boundaries(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    envelope = _outbound()
    assert store.enqueue_outbound(generation=generation, envelope=envelope).state == "pending"
    assert store.enqueue_outbound(generation=generation, envelope=envelope).state == "pending"
    store.begin_outbound_submission(generation=generation, outbound_id="outbound-1", now=201.0)
    store.mark_outbound_submitted(generation=generation, outbound_id="outbound-1", now=202.0)
    store.acknowledge_outbound(
        generation=generation,
        outbound_id="outbound-1",
        provider_message_id="provider-message-1",
        now=203.0,
    )
    record = store.get_outbound("outbound-1")
    assert record is not None
    assert record.state == "provider_acknowledged"
    assert record.provider_message_id == "provider-message-1"
    receipts = store.delivery_receipts("outbound-1")
    assert [receipt.stage for receipt in receipts] == [
        "gateway_accepted",
        "provider_submitted",
        "provider_acknowledged",
    ]


def test_monolithic_channel_ack_records_submission_and_ack_atomically(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    store.enqueue_outbound(generation=generation, envelope=_outbound())
    store.begin_outbound_submission(generation=generation, outbound_id="outbound-1")
    store.acknowledge_outbound(
        generation=generation,
        outbound_id="outbound-1",
        provider_message_id="provider-message-1",
        now=203.0,
    )
    assert [receipt.stage for receipt in store.delivery_receipts("outbound-1")] == [
        "gateway_accepted",
        "provider_submitted",
        "provider_acknowledged",
    ]


def test_event_replay_requires_resync_when_cursor_falls_outside_retained_window(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    for index in range(3):
        store.append_event(
            generation=generation,
            event="chat.update",
            payload={"index": index},
            now=float(index + 1),
        )
    assert store.prune_events(generation=generation, retain_last=2) == 1
    stale = store.replay_events(0)
    assert stale.resync_required is True
    assert stale.current_cursor == 3
    assert stale.events == ()
    replay = store.replay_events(1)
    assert replay.resync_required is False
    assert [event.seq for event in replay.events] == [2, 3]


@pytest.mark.parametrize("submitted", [False, True])
def test_restart_marks_possibly_sent_outbound_unknown(
    tmp_path: Path,
    submitted: bool,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first = store.acquire_writer_generation()
    store.enqueue_outbound(generation=first, envelope=_outbound())
    store.begin_outbound_submission(generation=first, outbound_id="outbound-1", now=201.0)
    if submitted:
        store.mark_outbound_submitted(
            generation=first,
            outbound_id="outbound-1",
            now=202.0,
        )
    second = store.acquire_writer_generation(now=203.0)
    record = store.get_outbound("outbound-1")
    assert record is not None
    assert record.state == "delivery_unknown"
    assert record.generation == second
    assert store.delivery_receipts("outbound-1")[-1].stage == "delivery_unknown"


def test_restart_marks_pending_outbound_definitely_not_submitted(tmp_path: Path) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first = store.acquire_writer_generation()
    store.enqueue_outbound(generation=first, envelope=_outbound())

    second = store.acquire_writer_generation(now=203.0)

    record = store.get_outbound("outbound-1")
    assert record is not None
    assert record.state == "failed"
    assert record.generation == second
    assert record.error_code == "gateway_restarted_before_submission"
    receipt = store.delivery_receipts("outbound-1")[-1]
    assert receipt.stage == "failed"
    assert receipt.error_code == "gateway_restarted_before_submission"


def test_existing_same_version_database_adds_session_and_run_tables(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    original = GatewayStateStore(root)
    with sqlite3.connect(original.database_path) as connection:
        connection.execute("DROP TABLE runs")
        connection.execute("DROP TABLE sessions")

    reopened = GatewayStateStore(root)
    generation = reopened.acquire_writer_generation()
    created = reopened.create_session(
        generation=generation,
        session_id="session-1",
        account=_account(),
        conversation=_conversation(),
    )
    assert created.session_id == "session-1"
    assert reopened.get_session("session-1") == created


def test_sessions_have_actor_independent_immutable_conversation_bindings(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    first = store.create_session(
        generation=generation,
        session_id="session-1",
        account=_account("account-1"),
        conversation=_conversation("shared-id"),
        mode="chat",
        debug=True,
        event_cursor=3,
        now=1.0,
    )
    duplicate = store.create_session(
        generation=generation,
        session_id="session-1",
        account=_account("account-1"),
        conversation=_conversation("shared-id"),
        mode="chat",
        debug=True,
        event_cursor=3,
        now=2.0,
    )
    assert duplicate == first
    assert (
        store.find_session_by_conversation(
            account=_account("account-1"),
            conversation=_conversation("shared-id"),
        )
        == first
    )
    assert "actor" not in {field.name for field in fields(SessionRecord)}
    assert "principal" not in {field.name for field in fields(SessionRecord)}

    with pytest.raises(SessionConflict, match="different state"):
        store.create_session(
            generation=generation,
            session_id="session-1",
            account=_account("account-1"),
            conversation=_conversation("shared-id"),
            mode="research",
        )
    with pytest.raises(SessionConflict, match="another session"):
        store.create_session(
            generation=generation,
            session_id="session-2",
            account=_account("account-1"),
            conversation=_conversation("shared-id"),
        )

    second_account = store.create_session(
        generation=generation,
        session_id="session-2",
        account=_account("account-2"),
        conversation=_conversation("shared-id"),
    )
    assert second_account.account.account_id == "account-2"
    assert [record.session_id for record in store.list_sessions()] == [
        "session-1",
        "session-2",
    ]


def test_session_patch_validates_mode_debug_cursor_and_writer_generation(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    first_generation = store.acquire_writer_generation()
    store.create_session(
        generation=first_generation,
        session_id="session-1",
        account=_account(),
        conversation=_conversation(),
    )
    patched = store.patch_session(
        generation=first_generation,
        session_id="session-1",
        mode="research.v1",
        debug=True,
        event_cursor=10,
        now=10.0,
    )
    assert (patched.mode, patched.debug, patched.event_cursor) == (
        "research.v1",
        True,
        10,
    )
    with pytest.raises(SessionConflict, match="cannot move backwards"):
        store.patch_session(
            generation=first_generation,
            session_id="session-1",
            event_cursor=9,
        )
    with pytest.raises(ValueError, match="mode"):
        store.patch_session(
            generation=first_generation,
            session_id="session-1",
            mode="not a mode",
        )
    with pytest.raises(ValueError, match="boolean"):
        store.patch_session(
            generation=first_generation,
            session_id="session-1",
            debug=1,  # type: ignore[arg-type]
        )

    second_generation = store.acquire_writer_generation()
    with pytest.raises(StaleWriterGeneration):
        store.patch_session(
            generation=first_generation,
            session_id="session-1",
            event_cursor=11,
        )
    refreshed = store.get_session("session-1")
    assert refreshed is not None
    assert refreshed.writer_generation == second_generation


def test_run_identity_is_idempotent_and_only_one_run_is_active(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    store.create_session(
        generation=generation,
        session_id="session-1",
        account=_account(),
        conversation=_conversation(),
    )
    accepted = store.begin_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
        input_fingerprint="a" * 64,
        now=1.0,
    )
    assert accepted.state == "accepted"
    assert (
        store.begin_run(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
            input_fingerprint="a" * 64,
            now=2.0,
        )
        == accepted
    )
    with pytest.raises(RunConflict, match="different input"):
        store.begin_run(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
            input_fingerprint="b" * 64,
        )
    with pytest.raises(RunConflict, match="active run"):
        store.begin_run(
            generation=generation,
            session_id="session-1",
            run_id="run-2",
            input_fingerprint="c" * 64,
        )
    running = store.start_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
        now=3.0,
    )
    assert running.state == "running"
    assert (
        store.start_run(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
            now=4.0,
        )
        == running
    )
    assert store.list_active_runs(session_id="session-1") == (running,)


def test_run_terminal_state_is_immutable_and_clears_session_binding(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    store.create_session(
        generation=generation,
        session_id="session-1",
        account=_account(),
        conversation=_conversation(),
    )
    store.begin_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
        input_fingerprint="a" * 64,
    )
    with pytest.raises(RunConflict, match="started"):
        store.finish_run(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
            outcome="completed",
        )
    store.start_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
    )
    completed = store.finish_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
        outcome="completed",
        result={"messageId": "message-1"},
        now=5.0,
    )
    assert completed.state == "completed"
    assert store.latest_run_for_session("session-1") == completed
    session = store.get_session("session-1")
    assert session is not None and session.active_run_id is None
    assert (
        store.finish_run(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
            outcome="completed",
            result={"messageId": "message-1"},
            now=6.0,
        )
        == completed
    )
    with pytest.raises(RunConflict, match="cannot be rewritten"):
        store.finish_run(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
            outcome="completed",
            result={"messageId": "different"},
        )
    assert (
        store.request_abort(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
        )
        == completed
    )
    next_run = store.begin_run(
        generation=generation,
        session_id="session-1",
        run_id="run-2",
        input_fingerprint="b" * 64,
    )
    assert next_run.state == "accepted"
    assert store.latest_run_for_session("session-1") == next_run


def test_abort_request_needs_real_worker_cancellation_and_handles_races(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(tmp_path / "state")
    generation = store.acquire_writer_generation()
    for index in range(2):
        store.create_session(
            generation=generation,
            session_id=f"session-{index}",
            account=_account(f"account-{index}"),
            conversation=_conversation(f"conversation-{index}"),
        )
        store.begin_run(
            generation=generation,
            session_id=f"session-{index}",
            run_id=f"run-{index}",
            input_fingerprint=str(index) * 64,
        )

    requested = store.request_abort(
        generation=generation,
        session_id="session-0",
        run_id="run-0",
        now=2.0,
    )
    assert requested.state == "abort_requested"
    with pytest.raises(RunConflict, match="worker cancelled"):
        store.finish_run(
            generation=generation,
            session_id="session-0",
            run_id="run-0",
            outcome="aborted",
        )
    aborted = store.finish_run(
        generation=generation,
        session_id="session-0",
        run_id="run-0",
        outcome="aborted",
        worker_stop_reason="cancelled",
    )
    assert aborted.state == "aborted"

    store.start_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
    )
    store.request_abort(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
    )
    completed = store.finish_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
        outcome="completed",
        result={"wonRace": "worker"},
    )
    assert completed.state == "completed"
    assert (
        store.request_abort(
            generation=generation,
            session_id="session-1",
            run_id="run-1",
        )
        == completed
    )


def test_restart_fences_runs_and_requires_explicit_terminal_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    stale_store = GatewayStateStore(root)
    first_generation = stale_store.acquire_writer_generation()
    states = ("accepted", "running", "abort_requested")
    for index, target_state in enumerate(states):
        session_id = f"session-{index}"
        run_id = f"run-{index}"
        stale_store.create_session(
            generation=first_generation,
            session_id=session_id,
            account=_account(f"account-{index}"),
            conversation=_conversation(f"conversation-{index}"),
        )
        stale_store.begin_run(
            generation=first_generation,
            session_id=session_id,
            run_id=run_id,
            input_fingerprint=str(index) * 64,
        )
        if target_state in {"running", "abort_requested"}:
            stale_store.start_run(
                generation=first_generation,
                session_id=session_id,
                run_id=run_id,
            )
        if target_state == "abort_requested":
            stale_store.request_abort(
                generation=first_generation,
                session_id=session_id,
                run_id=run_id,
            )

    reopened = GatewayStateStore(root)
    second_generation = reopened.acquire_writer_generation(now=20.0)
    recovered = reopened.list_active_runs()
    assert [record.state for record in recovered] == [
        "recovery_required",
        "recovery_required",
        "recovery_required",
    ]
    assert [record.recovery_from_state for record in recovered] == list(states)
    assert all(record.generation == second_generation for record in recovered)
    for index in range(3):
        session = reopened.get_session(f"session-{index}")
        assert session is not None
        assert session.active_run_id == f"run-{index}"
        assert session.writer_generation == second_generation

    with pytest.raises(StaleWriterGeneration):
        stale_store.resolve_run_recovery(
            generation=first_generation,
            session_id="session-0",
            run_id="run-0",
            outcome="failed",
            error_code="restart_interrupted",
        )
    with pytest.raises(RunConflict, match="active run"):
        reopened.begin_run(
            generation=second_generation,
            session_id="session-0",
            run_id="replacement",
            input_fingerprint="f" * 64,
        )
    with pytest.raises(RunConflict, match="unstarted"):
        reopened.resolve_run_recovery(
            generation=second_generation,
            session_id="session-0",
            run_id="run-0",
            outcome="completed",
        )
    reopened.resolve_run_recovery(
        generation=second_generation,
        session_id="session-0",
        run_id="run-0",
        outcome="failed",
        error_code="restart_interrupted",
    )
    reopened.resolve_run_recovery(
        generation=second_generation,
        session_id="session-1",
        run_id="run-1",
        outcome="completed",
        result={"reconciled": True},
    )
    with pytest.raises(RunConflict, match="worker cancelled"):
        reopened.resolve_run_recovery(
            generation=second_generation,
            session_id="session-2",
            run_id="run-2",
            outcome="aborted",
        )
    reopened.resolve_run_recovery(
        generation=second_generation,
        session_id="session-2",
        run_id="run-2",
        outcome="aborted",
        worker_stop_reason="cancelled",
    )
    assert reopened.list_active_runs() == ()
    assert all(
        reopened.get_session(f"session-{index}").active_run_id is None  # type: ignore[union-attr]
        for index in range(3)
    )
    assert (
        reopened.begin_run(
            generation=second_generation,
            session_id="session-0",
            run_id="replacement",
            input_fingerprint="f" * 64,
        ).state
        == "accepted"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file metadata contract")
def test_store_rejects_unsafe_root_mode_symlink_and_hardlinked_database(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(PermissionError, match="mode 0700"):
        GatewayStateStore(unsafe)

    unsafe_anchor = tmp_path / "unsafe-anchor"
    unsafe_anchor.mkdir(mode=0o755)
    with pytest.raises(PermissionError, match="trusted state anchor.*mode 0700"):
        GatewayStateStore(
            unsafe_anchor / "state",
            trusted_anchor=unsafe_anchor,
        )

    trusted_anchor = tmp_path / "trusted-anchor"
    trusted_anchor.mkdir(mode=0o700)
    with pytest.raises(GatewayStateError, match="inside its trusted anchor"):
        GatewayStateStore(
            tmp_path / "outside" / "state",
            trusted_anchor=trusted_anchor,
        )

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(GatewayStateError, match="symlink"):
        GatewayStateStore(link)

    store = GatewayStateStore(tmp_path / "safe")
    os.link(store.database_path, tmp_path / "database-copy")
    with pytest.raises(GatewayStateError, match="hard link"):
        store.current_writer_generation()
