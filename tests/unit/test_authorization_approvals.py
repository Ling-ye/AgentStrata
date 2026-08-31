from __future__ import annotations

from chatcopilot.authorization.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    generate_approval_challenge,
    hash_approval_challenge,
    is_valid_approval_challenge,
)
from chatcopilot.contracts.authorization import (
    ApprovalRequest,
    ApprovalResolution,
    stable_payload_digest,
)

_CHALLENGE = "approval_nonce_1234567890abcd"


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-1",
        session_id="session-1",
        run_id="run-1",
        operation="conversation.reset",
        target="current-conversation",
        params_digest=stable_payload_digest({"revision": "7"}),
        actor_ref="qq:actor-1",
        conversation_ref="qq:group:1",
        policy_version="policy-1",
        challenge_digest=hash_approval_challenge(_CHALLENGE),
        expires_at=100.0,
    )


def _resolution(**overrides) -> ApprovalResolution:
    values = {
        "approval_id": "approval-1",
        "actor_ref": "qq:actor-1",
        "conversation_ref": "qq:group:1",
        "params_digest": stable_payload_digest({"revision": "7"}),
        "policy_version": "policy-1",
        "challenge": _CHALLENGE,
        "accepted": True,
    }
    values.update(overrides)
    return ApprovalResolution(**values)


def test_approval_is_exact_and_one_shot() -> None:
    service = ApprovalService(InMemoryApprovalStore())
    assert service.issue(_request(), challenge=_CHALLENGE, now=10.0) is True

    first = service.resolve(_resolution(), now=50.0)
    replay = service.resolve(_resolution(), now=50.0)

    assert first.resolved is True
    assert first.accepted is True
    assert first.code == "approval-accepted"
    assert replay.resolved is False
    assert replay.code == "approval-already-resolved"


def test_approval_rejects_binding_drift_without_consuming_request() -> None:
    service = ApprovalService(InMemoryApprovalStore())
    service.issue(_request(), challenge=_CHALLENGE, now=10.0)

    mismatch = service.resolve(_resolution(actor_ref="qq:actor-2"), now=50.0)
    accepted = service.resolve(_resolution(), now=50.0)

    assert mismatch.code == "approval-actor-mismatch"
    assert accepted.resolved is True


def test_expired_or_policy_drifted_approval_fails_closed() -> None:
    expired_service = ApprovalService(InMemoryApprovalStore())
    expired_service.issue(_request(), challenge=_CHALLENGE, now=10.0)
    expired = expired_service.resolve(_resolution(), now=100.0)

    drift_service = ApprovalService(InMemoryApprovalStore())
    drift_service.issue(_request(), challenge=_CHALLENGE, now=10.0)
    drift = drift_service.resolve(_resolution(policy_version="policy-2"), now=50.0)

    assert expired.code == "approval-expired"
    assert drift.code == "approval-policy-drift"


def test_explicit_denial_consumes_request_without_claiming_mutation() -> None:
    service = ApprovalService(InMemoryApprovalStore())
    service.issue(_request(), challenge=_CHALLENGE, now=10.0)

    denied = service.resolve(_resolution(accepted=False), now=50.0)

    assert denied.resolved is True
    assert denied.accepted is False
    assert denied.code == "approval-denied"


def test_invalid_approval_request_is_not_issued() -> None:
    service = ApprovalService(InMemoryApprovalStore())
    request = _request()
    invalid = ApprovalRequest(
        approval_id=request.approval_id,
        session_id=request.session_id,
        run_id=request.run_id,
        operation=request.operation,
        target=request.target,
        params_digest=request.params_digest,
        actor_ref=request.actor_ref,
        conversation_ref=request.conversation_ref,
        policy_version=request.policy_version,
        challenge_digest="",
        expires_at=request.expires_at,
    )

    assert service.issue(invalid, challenge=_CHALLENGE, now=10.0) is False


def test_issue_rejects_challenge_drift_and_non_opaque_values() -> None:
    service = ApprovalService(InMemoryApprovalStore())

    assert service.issue(_request(), challenge="too-short", now=10.0) is False
    assert (
        service.issue(
            _request(),
            challenge="different_nonce_1234567890ab",
            now=10.0,
        )
        is False
    )


def test_generated_challenge_is_opaque_and_url_safe() -> None:
    challenge = generate_approval_challenge()

    assert is_valid_approval_challenge(challenge)
    assert len(challenge) >= 24
