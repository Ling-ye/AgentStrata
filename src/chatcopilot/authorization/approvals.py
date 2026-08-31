"""One-shot approval resolution bound to trusted operation facts."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
from typing import Protocol

from chatcopilot.contracts.authorization import (
    ApprovalReceipt,
    ApprovalRequest,
    ApprovalResolution,
)


def hash_approval_challenge(challenge: str) -> str:
    return "sha256:" + hashlib.sha256(challenge.encode("utf-8")).hexdigest()


_APPROVAL_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")


def generate_approval_challenge() -> str:
    """Return an opaque confirmation value safe to display to an approval client."""

    return secrets.token_urlsafe(24)


def is_valid_approval_challenge(challenge: object) -> bool:
    return isinstance(challenge, str) and _APPROVAL_CHALLENGE_RE.fullmatch(challenge) is not None


class ApprovalStore(Protocol):
    def create(
        self,
        request: ApprovalRequest,
        *,
        challenge: str,
        created_at: float,
    ) -> bool: ...

    def get(self, approval_id: str) -> ApprovalRequest | None: ...

    def resolve_once(
        self,
        request: ApprovalRequest,
        resolution: ApprovalResolution,
        *,
        decision_id: str,
        decided_at: float,
    ) -> bool: ...


class InMemoryApprovalStore:
    """Test/local implementation; the Gateway store supplies durable semantics."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._resolved: dict[str, str] = {}
        self._challenges: dict[str, str] = {}

    def create(
        self,
        request: ApprovalRequest,
        *,
        challenge: str,
        created_at: float,
    ) -> bool:
        del created_at
        if request.approval_id in self._requests:
            return False
        self._requests[request.approval_id] = request
        self._challenges[request.approval_id] = challenge
        return True

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._requests.get(approval_id)

    def resolve_once(
        self,
        request: ApprovalRequest,
        resolution: ApprovalResolution,
        *,
        decision_id: str,
        decided_at: float,
    ) -> bool:
        del decided_at
        approval_id = request.approval_id
        stored = self._requests.get(approval_id)
        if (
            stored != request
            or approval_id in self._resolved
            or self._challenges.get(approval_id) != resolution.challenge
        ):
            return False
        self._resolved[approval_id] = decision_id
        self._challenges.pop(approval_id, None)
        return True


class ApprovalService:
    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    def issue(
        self,
        request: ApprovalRequest,
        *,
        challenge: str,
        now: float | None = None,
    ) -> bool:
        created_at = _timestamp(now)
        if not all(
            (
                request.approval_id,
                request.session_id,
                request.operation,
                request.target,
                request.params_digest,
                request.actor_ref,
                request.conversation_ref,
                request.policy_version,
            )
        ):
            return False
        if request.run_id is not None and not request.run_id:
            return False
        if not _valid_prefixed_sha256(request.params_digest):
            return False
        if (
            type(request.expires_at) not in {int, float}
            or not math.isfinite(request.expires_at)
            or request.expires_at <= created_at
        ):
            return False
        if not _valid_prefixed_sha256(request.challenge_digest):
            return False
        if not is_valid_approval_challenge(challenge):
            return False
        if not hmac.compare_digest(
            hash_approval_challenge(challenge),
            request.challenge_digest,
        ):
            return False
        return self._store.create(
            request,
            challenge=challenge,
            created_at=created_at,
        )

    def resolve(
        self,
        resolution: ApprovalResolution,
        *,
        now: float | None = None,
    ) -> ApprovalReceipt:
        decided_at = _timestamp(now)
        request = self._store.get(resolution.approval_id)
        if request is None:
            return self._receipt(resolution, code="approval-not-found")
        if decided_at >= request.expires_at:
            return self._receipt(resolution, code="approval-expired")
        if resolution.actor_ref != request.actor_ref:
            return self._receipt(resolution, code="approval-actor-mismatch")
        if resolution.conversation_ref != request.conversation_ref:
            return self._receipt(resolution, code="approval-conversation-mismatch")
        if resolution.params_digest != request.params_digest:
            return self._receipt(resolution, code="approval-params-mismatch")
        if resolution.policy_version != request.policy_version:
            return self._receipt(resolution, code="approval-policy-drift")
        if type(resolution.accepted) is not bool:
            return self._receipt(resolution, code="approval-decision-invalid")
        if not is_valid_approval_challenge(resolution.challenge):
            return self._receipt(resolution, code="approval-challenge-mismatch")
        challenge_digest = hash_approval_challenge(resolution.challenge)
        if not hmac.compare_digest(challenge_digest, request.challenge_digest):
            return self._receipt(resolution, code="approval-challenge-mismatch")

        decision_id = self._decision_id(request, resolution)
        if not self._store.resolve_once(
            request,
            resolution,
            decision_id=decision_id,
            decided_at=decided_at,
        ):
            return ApprovalReceipt(
                approval_id=request.approval_id,
                decision_id=decision_id,
                resolved=False,
                accepted=False,
                code="approval-already-resolved",
            )
        return ApprovalReceipt(
            approval_id=request.approval_id,
            decision_id=decision_id,
            resolved=True,
            accepted=resolution.accepted,
            code="approval-accepted" if resolution.accepted else "approval-denied",
        )

    @staticmethod
    def _decision_id(request: ApprovalRequest, resolution: ApprovalResolution) -> str:
        digest = hashlib.sha256(
            (
                request.approval_id
                + "\0"
                + request.session_id
                + "\0"
                + request.operation
                + "\0"
                + request.target
                + "\0"
                + request.params_digest
                + "\0"
                + resolution.actor_ref
                + "\0"
                + resolution.conversation_ref
                + "\0"
                + resolution.policy_version
                + "\0"
                + str(resolution.accepted)
            ).encode("utf-8")
        ).hexdigest()[:24]
        return "approval_" + digest

    @staticmethod
    def _receipt(resolution: ApprovalResolution, *, code: str) -> ApprovalReceipt:
        return ApprovalReceipt(
            approval_id=resolution.approval_id,
            decision_id="",
            resolved=False,
            accepted=False,
            code=code,
        )


def _timestamp(value: float | None) -> float:
    result = time.time() if value is None else value
    if type(result) not in {int, float} or not math.isfinite(result) or result < 0:
        raise ValueError("approval timestamp is invalid")
    return float(result)


def _valid_prefixed_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value[7:]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


__all__ = [
    "ApprovalService",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "generate_approval_challenge",
    "hash_approval_challenge",
    "is_valid_approval_challenge",
]
