"""Ordered, identity-bound, HMAC-signed Canary observer receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
from typing import Any

from ._fs import canonical_json, read_private_json, write_new_private_json
from .errors import CanaryConflictError, CanaryIntegrityError, CanaryStateError
from .state import CanaryPhase, CanaryStateMachine
from .target import CanaryTargetFactory, CanaryTargetHandle


_RECEIPT_FILE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<phase>[a-z_]+)\.json$")


@dataclass(frozen=True, slots=True)
class CanaryReceipt:
    schema_version: int
    sequence: int
    phase: CanaryPhase
    evaluation_id: str
    trial_id: str
    target_id: str
    observed_at: str
    previous_digest: str
    evidence: dict[str, Any]
    digest: str
    signature: str


class CanaryReceiptWriter:
    def __init__(
        self,
        factory: CanaryTargetFactory,
        handle: CanaryTargetHandle,
        *,
        signing_key: bytes | None = None,
    ) -> None:
        self.factory = factory
        self.handle = factory.validate_handle(handle)
        self.signing_key = signing_key or secrets.token_bytes(32)
        if len(self.signing_key) < 32:
            raise ValueError("Canary receipt HMAC key must contain at least 32 bytes")
        existing = tuple(self.handle.receipts_root.iterdir())
        if existing:
            raise CanaryConflictError("Canary receipt writer requires an empty receipt root")
        self._machine = CanaryStateMachine()
        self._sequence = 0
        self._previous_digest = ""

    def append(
        self,
        phase: CanaryPhase,
        evidence: dict[str, Any],
        *,
        observed_at: str | None = None,
    ) -> CanaryReceipt:
        if not isinstance(evidence, dict):
            raise TypeError("Canary receipt evidence must be an object")
        self._machine.advance(phase)
        sequence = self._sequence + 1
        unsigned = {
            "schema_version": 1,
            "sequence": sequence,
            "phase": phase.value,
            "evaluation_id": self.handle.evaluation_id,
            "trial_id": self.handle.trial_id,
            "target_id": self.handle.target_id,
            "observed_at": observed_at or _utc_now(),
            "previous_digest": self._previous_digest,
            "evidence": evidence,
        }
        digest = hashlib.sha256(canonical_json(unsigned)).hexdigest()
        signature = hmac.new(self.signing_key, digest.encode("ascii"), hashlib.sha256).hexdigest()
        receipt = CanaryReceipt(
            schema_version=1,
            sequence=sequence,
            phase=phase,
            evaluation_id=self.handle.evaluation_id,
            trial_id=self.handle.trial_id,
            target_id=self.handle.target_id,
            observed_at=str(unsigned["observed_at"]),
            previous_digest=self._previous_digest,
            evidence=evidence,
            digest=digest,
            signature=signature,
        )
        path = self.handle.receipts_root / f"{sequence:06d}-{phase.value}.json"
        write_new_private_json(
            path,
            _receipt_payload(receipt),
            root=self.handle.private_root,
        )
        self._sequence = sequence
        self._previous_digest = digest
        return receipt


class CanaryReceiptVerifier:
    def __init__(
        self,
        factory: CanaryTargetFactory,
        handle: CanaryTargetHandle,
        *,
        signing_key: bytes,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("Canary receipt HMAC key must contain at least 32 bytes")
        self.factory = factory
        self.handle = factory.validate_handle(handle)
        self.signing_key = signing_key

    def verify(self) -> tuple[CanaryReceipt, ...]:
        self.factory.validate_handle(self.handle)
        paths = sorted(self.handle.receipts_root.iterdir(), key=lambda item: item.name)
        if not paths:
            raise CanaryIntegrityError("Canary receipt chain is empty")
        machine = CanaryStateMachine()
        previous_digest = ""
        receipts: list[CanaryReceipt] = []
        for expected_sequence, path in enumerate(paths, start=1):
            match = _RECEIPT_FILE.fullmatch(path.name)
            if match is None:
                raise CanaryIntegrityError(f"unexpected Canary receipt artifact: {path.name}")
            payload = read_private_json(path, root=self.handle.private_root)
            receipt = _receipt_from_payload(payload)
            if int(match.group("sequence")) != expected_sequence:
                raise CanaryIntegrityError("Canary receipt filename sequence is not contiguous")
            if receipt.sequence != expected_sequence or receipt.phase.value != match.group("phase"):
                raise CanaryIntegrityError("Canary receipt filename and payload do not match")
            if (
                receipt.schema_version != 1
                or receipt.evaluation_id != self.handle.evaluation_id
                or receipt.trial_id != self.handle.trial_id
                or receipt.target_id != self.handle.target_id
            ):
                raise CanaryIntegrityError("Canary receipt target binding is invalid")
            if not hmac.compare_digest(receipt.previous_digest, previous_digest):
                raise CanaryIntegrityError("Canary receipt previous digest does not match")
            unsigned = _receipt_payload(receipt, include_integrity=False)
            expected_digest = hashlib.sha256(canonical_json(unsigned)).hexdigest()
            expected_signature = hmac.new(
                self.signing_key,
                expected_digest.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(receipt.digest, expected_digest):
                raise CanaryIntegrityError("Canary receipt digest does not match")
            if not hmac.compare_digest(receipt.signature, expected_signature):
                raise CanaryIntegrityError("Canary receipt signature does not match")
            try:
                machine.advance(receipt.phase)
            except CanaryStateError as exc:
                raise CanaryIntegrityError("Canary receipt phase sequence is invalid") from exc
            previous_digest = receipt.digest
            receipts.append(receipt)
        return tuple(receipts)


def _receipt_payload(
    receipt: CanaryReceipt,
    *,
    include_integrity: bool = True,
) -> dict[str, Any]:
    payload = asdict(receipt)
    payload["phase"] = receipt.phase.value
    if not include_integrity:
        payload.pop("digest", None)
        payload.pop("signature", None)
    return payload


def _receipt_from_payload(payload: dict[str, Any]) -> CanaryReceipt:
    try:
        evidence = payload["evidence"]
        if not isinstance(evidence, dict):
            raise TypeError("evidence must be an object")
        return CanaryReceipt(
            schema_version=int(payload["schema_version"]),
            sequence=int(payload["sequence"]),
            phase=CanaryPhase(str(payload["phase"])),
            evaluation_id=str(payload["evaluation_id"]),
            trial_id=str(payload["trial_id"]),
            target_id=str(payload["target_id"]),
            observed_at=str(payload["observed_at"]),
            previous_digest=str(payload["previous_digest"]),
            evidence=evidence,
            digest=str(payload["digest"]),
            signature=str(payload["signature"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryIntegrityError("Canary receipt payload is invalid") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["CanaryReceipt", "CanaryReceiptVerifier", "CanaryReceiptWriter"]
