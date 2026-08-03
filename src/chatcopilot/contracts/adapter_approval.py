"""Immutable approval contract for repository-native external-tool adapters."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlparse


SUPPORTED_ADAPTER_FORGES = frozenset(
    {"bitbucket.org", "codeberg.org", "github.com", "gitlab.com"}
)


@dataclass(frozen=True)
class AdapterApprovalEnvelope:
    resource_name: str
    source_url: str
    approved_ref: str
    license_evidence: str
    integration_intent: str

    def canonical_payload(self) -> dict[str, str]:
        return {
            "schema": "agentstrata-adapter-approval-v1",
            "resource_name": self.resource_name.strip(),
            "source_url": self.source_url.strip(),
            "approved_ref": self.approved_ref.strip(),
            "license_evidence": self.license_evidence.strip(),
            "integration_intent": self.integration_intent.strip(),
        }

    @property
    def candidate_digest(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_adapter_approval(envelope: AdapterApprovalEnvelope) -> tuple[str, ...]:
    payload = envelope.canonical_payload()
    errors: list[str] = []
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", payload["resource_name"]):
        errors.append("resource_name must be a stable adapter identifier")

    parsed = urlparse(payload["source_url"])
    repository_parts = tuple(part for part in parsed.path.split("/") if part)
    try:
        source_port = parsed.port
    except ValueError:
        source_port = -1
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or hostname not in SUPPORTED_ADAPTER_FORGES
        or parsed.username
        or parsed.password
        or source_port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or len(repository_parts) < 2
        or (hostname != "gitlab.com" and len(repository_parts) != 2)
    ):
        errors.append(
            "source_url must be an HTTPS repository on a supported public forge"
        )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", payload["approved_ref"]):
        errors.append("approved_ref must be a full immutable Git commit SHA")
    if not payload["license_evidence"]:
        errors.append("license_evidence is required")
    if not payload["integration_intent"]:
        errors.append("integration_intent is required")
    return tuple(errors)


__all__ = [
    "AdapterApprovalEnvelope",
    "SUPPORTED_ADAPTER_FORGES",
    "validate_adapter_approval",
]
