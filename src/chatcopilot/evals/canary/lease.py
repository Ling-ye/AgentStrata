"""Persistent, signed target-level Canary deployment leases."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import secrets
from typing import Any

from ._fs import atomic_write_private_json, canonical_json, read_private_json, validate_private_file
from .errors import CanaryConflictError, CanaryIntegrityError, CanaryStateError
from .target import CanaryTargetFactory, CanaryTargetHandle


class LeaseState(str, Enum):
    LEASED = "leased"
    ACTIVATING = "activating"
    VERIFYING = "verifying"
    RESTORING = "restoring"
    CLEANUP = "cleanup"
    QUARANTINED = "quarantined"


_LEASE_TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.LEASED: frozenset(
        {LeaseState.ACTIVATING, LeaseState.CLEANUP, LeaseState.QUARANTINED}
    ),
    LeaseState.ACTIVATING: frozenset(
        {LeaseState.VERIFYING, LeaseState.RESTORING, LeaseState.QUARANTINED}
    ),
    LeaseState.VERIFYING: frozenset({LeaseState.RESTORING, LeaseState.QUARANTINED}),
    LeaseState.RESTORING: frozenset({LeaseState.CLEANUP, LeaseState.QUARANTINED}),
    LeaseState.CLEANUP: frozenset({LeaseState.QUARANTINED}),
    LeaseState.QUARANTINED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CanaryDeploymentLease:
    schema_version: int
    lease_id: str
    evaluation_id: str
    trial_id: str
    target_id: str
    observer_unit: str
    observer_pid: int
    observer_start_time: str
    baseline_generation: str
    candidate_generation: str
    candidate_digest: str
    state: LeaseState
    created_at: str
    last_heartbeat_at: str
    signature: str = ""

    @classmethod
    def create(
        cls,
        *,
        handle: CanaryTargetHandle,
        observer_unit: str,
        observer_pid: int,
        observer_start_time: str,
        baseline_generation: str,
        candidate_generation: str,
        candidate_digest: str,
        now: str | None = None,
    ) -> "CanaryDeploymentLease":
        timestamp = now or _utc_now()
        return cls(
            schema_version=1,
            lease_id=secrets.token_hex(16),
            evaluation_id=handle.evaluation_id,
            trial_id=handle.trial_id,
            target_id=handle.target_id,
            observer_unit=observer_unit,
            observer_pid=observer_pid,
            observer_start_time=observer_start_time,
            baseline_generation=baseline_generation,
            candidate_generation=candidate_generation,
            candidate_digest=candidate_digest,
            state=LeaseState.LEASED,
            created_at=timestamp,
            last_heartbeat_at=timestamp,
        )


class CanaryLeaseStore:
    def __init__(
        self,
        factory: CanaryTargetFactory,
        handle: CanaryTargetHandle,
        *,
        signing_key: bytes,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("Canary lease HMAC key must contain at least 32 bytes")
        self.factory = factory
        self.handle = factory.validate_handle(handle)
        self.signing_key = signing_key
        self.path = self.handle.control_root / "deployment-lease.json"

    def acquire(self, lease: CanaryDeploymentLease) -> CanaryDeploymentLease:
        self.factory.validate_handle(self.handle)
        if self.path.exists() or self.path.is_symlink():
            # Any existing lease, including an apparently stale one, requires
            # reconciliation. It is never stolen on time alone.
            self.load()
            raise CanaryConflictError("Canary deployment lease is already active")
        self._validate_identity(lease)
        if lease.state != LeaseState.LEASED:
            raise CanaryStateError("a new Canary deployment lease must start in leased state")
        signed = self._signed(lease)
        atomic_write_private_json(self.path, _lease_payload(signed), root=self.handle.private_root)
        return self.load()

    def load(self) -> CanaryDeploymentLease:
        payload = read_private_json(self.path, root=self.handle.private_root)
        lease = _lease_from_payload(payload)
        self._validate_identity(lease)
        expected = self._signature(replace(lease, signature=""))
        if not hmac.compare_digest(lease.signature, expected):
            raise CanaryIntegrityError("Canary deployment lease signature does not match")
        return lease

    def transition(
        self,
        lease_id: str,
        next_state: LeaseState,
        *,
        now: str | None = None,
    ) -> CanaryDeploymentLease:
        current = self.load()
        if not hmac.compare_digest(current.lease_id, lease_id):
            raise CanaryConflictError("Canary deployment lease ID does not match")
        if next_state not in _LEASE_TRANSITIONS[current.state]:
            raise CanaryStateError(
                f"invalid Canary lease transition: {current.state.value} -> {next_state.value}"
            )
        updated = replace(
            current,
            state=next_state,
            last_heartbeat_at=now or _utc_now(),
            signature="",
        )
        signed = self._signed(updated)
        atomic_write_private_json(self.path, _lease_payload(signed), root=self.handle.private_root)
        return self.load()

    def heartbeat(self, lease_id: str, *, now: str | None = None) -> CanaryDeploymentLease:
        current = self.load()
        if not hmac.compare_digest(current.lease_id, lease_id):
            raise CanaryConflictError("Canary deployment lease ID does not match")
        if current.state == LeaseState.QUARANTINED:
            raise CanaryStateError("a quarantined Canary lease cannot be renewed")
        updated = replace(
            current,
            last_heartbeat_at=now or _utc_now(),
            signature="",
        )
        signed = self._signed(updated)
        atomic_write_private_json(self.path, _lease_payload(signed), root=self.handle.private_root)
        return self.load()

    def release(self, lease_id: str) -> None:
        current = self.load()
        if not hmac.compare_digest(current.lease_id, lease_id):
            raise CanaryConflictError("Canary deployment lease ID does not match")
        if current.state != LeaseState.CLEANUP:
            raise CanaryStateError("Canary lease can be released only after cleanup")
        validate_private_file(self.path, root=self.handle.private_root)
        self.path.unlink()

    def _signed(self, lease: CanaryDeploymentLease) -> CanaryDeploymentLease:
        return replace(lease, signature=self._signature(replace(lease, signature="")))

    def _signature(self, lease: CanaryDeploymentLease) -> str:
        return hmac.new(
            self.signing_key,
            canonical_json(_lease_payload(lease, include_signature=False)),
            hashlib.sha256,
        ).hexdigest()

    def _validate_identity(self, lease: CanaryDeploymentLease) -> None:
        if (
            lease.schema_version != 1
            or lease.evaluation_id != self.handle.evaluation_id
            or lease.trial_id != self.handle.trial_id
            or lease.target_id != self.handle.target_id
            or lease.observer_unit != self.handle.unit_name
            or lease.observer_pid <= 0
            or not lease.observer_start_time
            or not lease.baseline_generation
            or not lease.candidate_generation
            or len(lease.candidate_digest) != 64
        ):
            raise CanaryIntegrityError("Canary deployment lease identity is invalid")


def _lease_payload(
    lease: CanaryDeploymentLease,
    *,
    include_signature: bool = True,
) -> dict[str, Any]:
    payload = asdict(lease)
    payload["state"] = lease.state.value
    if not include_signature:
        payload.pop("signature", None)
    return payload


def _lease_from_payload(payload: dict[str, Any]) -> CanaryDeploymentLease:
    try:
        return CanaryDeploymentLease(
            schema_version=int(payload["schema_version"]),
            lease_id=str(payload["lease_id"]),
            evaluation_id=str(payload["evaluation_id"]),
            trial_id=str(payload["trial_id"]),
            target_id=str(payload["target_id"]),
            observer_unit=str(payload["observer_unit"]),
            observer_pid=int(payload["observer_pid"]),
            observer_start_time=str(payload["observer_start_time"]),
            baseline_generation=str(payload["baseline_generation"]),
            candidate_generation=str(payload["candidate_generation"]),
            candidate_digest=str(payload["candidate_digest"]),
            state=LeaseState(str(payload["state"])),
            created_at=str(payload["created_at"]),
            last_heartbeat_at=str(payload["last_heartbeat_at"]),
            signature=str(payload["signature"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryIntegrityError("Canary deployment lease payload is invalid") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["CanaryDeploymentLease", "CanaryLeaseStore", "LeaseState"]
