from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from chatcopilot.authorization.approvals import hash_approval_challenge
from chatcopilot.contracts.authorization import (
    ApprovalRequest,
    ApprovalResolution,
    stable_payload_digest,
)
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.gateway import (
    ApprovalConflict,
    GatewayApprovalService,
    GatewayStateStore,
    StaleWriterGeneration,
)
from chatcopilot.gateway.protocol import GATEWAY_METHODS


_ACTOR = "qq:actor-1"
_CONVERSATION = "qq:group:20001"
_CHALLENGE = "approval_nonce_1234567890abcd"


def _state(root: Path) -> tuple[GatewayStateStore, int]:
    state = GatewayStateStore(root)
    generation = state.acquire_writer_generation(now=1.0)
    state.create_session(
        generation=generation,
        session_id="session-1",
        account=ChannelAccountRef("qq", "10001"),
        conversation=ConversationRef("group", "20001"),
        now=2.0,
    )
    state.begin_run(
        generation=generation,
        session_id="session-1",
        run_id="run-1",
        input_fingerprint="a" * 64,
        now=3.0,
    )
    return state, generation


def _request(
    *,
    approval_id: str = "approval-1",
    challenge: str = _CHALLENGE,
    params_digest: str | None = None,
    policy_version: str = "policy-v1",
    expires_at: float = 100.0,
) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        session_id="session-1",
        run_id="run-1",
        operation="conversation.reset",
        target="current-conversation",
        params_digest=params_digest
        or stable_payload_digest({"target_revision": "7"}),
        actor_ref=_ACTOR,
        conversation_ref=_CONVERSATION,
        policy_version=policy_version,
        challenge_digest=hash_approval_challenge(challenge),
        expires_at=expires_at,
    )


def _resolution(
    *,
    actor_ref: str = _ACTOR,
    conversation_ref: str = _CONVERSATION,
    params_digest: str | None = None,
    policy_version: str = "policy-v1",
    challenge: str = _CHALLENGE,
    accepted: bool = True,
) -> ApprovalResolution:
    return ApprovalResolution(
        approval_id="approval-1",
        actor_ref=actor_ref,
        conversation_ref=conversation_ref,
        params_digest=params_digest
        or stable_payload_digest({"target_revision": "7"}),
        policy_version=policy_version,
        challenge=challenge,
        accepted=accepted,
    )


def test_pending_approval_and_challenge_survive_gateway_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    first_state, first_generation = _state(root)
    first = GatewayApprovalService(first_state, generation=first_generation)
    assert first.issue(_request(), challenge=_CHALLENGE, now=10.0)

    reopened = GatewayStateStore(root)
    second_generation = reopened.acquire_writer_generation(now=20.0)
    recovered = GatewayApprovalService(reopened, generation=second_generation)
    snapshot = recovered.get(
        "approval-1",
        actor_ref=_ACTOR,
        conversation_ref=_CONVERSATION,
        session_id="session-1",
        now=21.0,
    )

    assert snapshot is not None
    assert snapshot.status == "pending"
    assert snapshot.challenge == _CHALLENGE
    assert snapshot.allowed_decisions == ("approve", "deny")
    record = reopened.get_approval(
        "approval-1",
        actor_ref=_ACTOR,
        conversation_ref=_CONVERSATION,
        session_id="session-1",
        now=21.0,
    )
    assert record is not None
    assert _CHALLENGE not in repr(record)


def test_resolution_is_atomic_one_shot_and_erases_plaintext_challenge(
    tmp_path: Path,
) -> None:
    state, generation = _state(tmp_path / "state")
    service = GatewayApprovalService(state, generation=generation)
    assert service.issue(_request(), challenge=_CHALLENGE, now=10.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(
            pool.map(
                lambda _: service.resolve(_resolution(), now=50.0),
                range(2),
            )
        )

    assert sum(receipt.resolved for receipt in receipts) == 1
    assert {receipt.code for receipt in receipts} == {
        "approval-accepted",
        "approval-already-resolved",
    }
    snapshot = service.get(
        "approval-1",
        actor_ref=_ACTOR,
        conversation_ref=_CONVERSATION,
        session_id="session-1",
        now=51.0,
    )
    assert snapshot is not None
    assert snapshot.status == "resolved"
    assert snapshot.challenge is None
    assert snapshot.allowed_decisions == ()
    with sqlite3.connect(state.database_path) as connection:
        challenge, status = connection.execute(
            "SELECT challenge, state FROM approvals WHERE approval_id = 'approval-1'"
        ).fetchone()
    assert (challenge, status) == ("", "resolved")


def test_cross_binding_queries_are_non_enumerating_and_do_not_consume(
    tmp_path: Path,
) -> None:
    state, generation = _state(tmp_path / "state")
    service = GatewayApprovalService(state, generation=generation)
    service.issue(_request(), challenge=_CHALLENGE, now=10.0)

    assert (
        service.get(
            "approval-1",
            actor_ref="qq:actor-2",
            conversation_ref=_CONVERSATION,
            session_id="session-1",
            now=20.0,
        )
        is None
    )
    hidden, next_cursor = service.list(
        actor_ref=_ACTOR,
        conversation_ref="qq:group:other",
        now=20.0,
    )
    assert hidden == ()
    assert next_cursor is None

    mismatch = service.resolve(
        _resolution(actor_ref="qq:actor-2"),
        now=50.0,
    )
    accepted = service.resolve(_resolution(), now=50.0)
    assert mismatch.code == "approval-actor-mismatch"
    assert accepted.code == "approval-accepted"


@pytest.mark.parametrize(
    ("resolution", "code"),
    [
        (
            _resolution(
                params_digest=stable_payload_digest({"target_revision": "8"})
            ),
            "approval-params-mismatch",
        ),
        (_resolution(policy_version="policy-v2"), "approval-policy-drift"),
        (
            _resolution(challenge="different_nonce_1234567890ab"),
            "approval-challenge-mismatch",
        ),
        (
            _resolution(conversation_ref="qq:group:other"),
            "approval-conversation-mismatch",
        ),
    ],
)
def test_resolution_drift_fails_without_consuming(
    tmp_path: Path,
    resolution: ApprovalResolution,
    code: str,
) -> None:
    state, generation = _state(tmp_path / "state")
    service = GatewayApprovalService(state, generation=generation)
    service.issue(_request(), challenge=_CHALLENGE, now=10.0)

    assert service.resolve(resolution, now=50.0).code == code
    assert service.resolve(_resolution(), now=50.0).resolved


def test_expiry_is_durable_and_redacts_challenge(tmp_path: Path) -> None:
    state, generation = _state(tmp_path / "state")
    service = GatewayApprovalService(state, generation=generation)
    service.issue(_request(), challenge=_CHALLENGE, now=10.0)

    snapshot = service.get(
        "approval-1",
        actor_ref=_ACTOR,
        conversation_ref=_CONVERSATION,
        session_id="session-1",
        now=100.0,
    )

    assert snapshot is not None
    assert snapshot.status == "expired"
    assert snapshot.challenge is None
    assert snapshot.allowed_decisions == ()
    assert service.resolve(_resolution(), now=100.0).code == "approval-expired"
    with sqlite3.connect(state.database_path) as connection:
        challenge, status = connection.execute(
            "SELECT challenge, state FROM approvals WHERE approval_id = 'approval-1'"
        ).fetchone()
    assert (challenge, status) == ("", "expired")


def test_duplicate_identity_is_idempotent_only_for_exact_pending_request(
    tmp_path: Path,
) -> None:
    state, generation = _state(tmp_path / "state")
    service = GatewayApprovalService(state, generation=generation)
    request = _request()

    assert service.issue(request, challenge=_CHALLENGE, now=10.0)
    assert not service.issue(request, challenge=_CHALLENGE, now=11.0)
    with pytest.raises(ApprovalConflict):
        service.issue(
            _request(policy_version="policy-v2"),
            challenge=_CHALLENGE,
            now=11.0,
        )


def test_stale_gateway_generation_cannot_issue_or_resolve(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state, first_generation = _state(root)
    stale = GatewayApprovalService(state, generation=first_generation)
    stale.issue(_request(), challenge=_CHALLENGE, now=10.0)
    state.acquire_writer_generation(now=20.0)

    with pytest.raises(StaleWriterGeneration):
        stale.issue(
            _request(approval_id="approval-2"),
            challenge=_CHALLENGE,
            now=21.0,
        )
    with pytest.raises(StaleWriterGeneration):
        stale.resolve(_resolution(), now=50.0)


def test_rpc_surface_has_no_client_approval_creation_method() -> None:
    assert "approvals.create" not in GATEWAY_METHODS
