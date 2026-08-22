"""Pure result types for the private session-attestation handoff."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionAttestationResultKind(str, Enum):
    """Outcome of matching one queued transport record."""

    MATCHED = "matched"
    MISSING = "missing"
    ACTOR_MISMATCH = "actor_mismatch"
    CONTENT_MISMATCH = "content_mismatch"


@dataclass(frozen=True)
class SessionAttestationConsumeResult:
    """Store-owned match result without platform-specific authorization text."""

    kind: SessionAttestationResultKind
    content_digest_matches: bool = False


__all__ = [
    "SessionAttestationConsumeResult",
    "SessionAttestationResultKind",
]
