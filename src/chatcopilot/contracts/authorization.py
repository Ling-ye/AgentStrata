"""Platform-neutral authorization and approval contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from chatcopilot.contracts.identity import ConversationIdentity, Role


class AuthorizationOperation(str, Enum):
    INGRESS = "ingress"
    COMMAND = "command"
    RESOURCE = "resource"
    TOOL = "tool"
    WORKSPACE = "workspace"
    LIFECYCLE = "lifecycle"
    APPROVAL = "approval"


def stable_payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Host-confirmed caller identity; Channel claims alone are not principals."""

    channel: str
    account_id: str
    conversation: ConversationIdentity
    user_id: str
    role: Role
    evidence_digest: str

    @property
    def actor_ref(self) -> str:
        return self.conversation.platform + ":" + stable_payload_digest(
            {
                "account_id": self.account_id,
                "chat_id": self.conversation.chat_id,
                "chat_kind": self.conversation.chat_kind,
                "user_id": self.user_id,
            }
        )[7:31]


@dataclass(frozen=True)
class AuthorizationRequest:
    request_id: str
    principal: Principal
    operation: AuthorizationOperation
    target: str
    params_digest: str

    @property
    def request_digest(self) -> str:
        return stable_payload_digest(
            {
                "actor_ref": self.principal.actor_ref,
                "operation": self.operation.value,
                "params_digest": self.params_digest,
                "request_id": self.request_id,
                "target": self.target,
            }
        )


@dataclass(frozen=True)
class AuthorizationDecision:
    decision_id: str
    request_id: str
    request_digest: str
    allowed: bool
    code: str
    policy_version: str
    actor_ref: str


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    session_id: str
    operation: str
    target: str
    params_digest: str
    actor_ref: str
    conversation_ref: str
    policy_version: str
    challenge_digest: str
    expires_at: float
    run_id: str | None = None


@dataclass(frozen=True)
class ApprovalResolution:
    approval_id: str
    actor_ref: str
    conversation_ref: str
    params_digest: str
    policy_version: str
    challenge: str
    accepted: bool


@dataclass(frozen=True)
class ApprovalReceipt:
    approval_id: str
    decision_id: str
    resolved: bool
    accepted: bool
    code: str


@dataclass(frozen=True)
class MutationReceipt:
    """Only ``committed`` proves the domain mutation reached durable state."""

    receipt_id: str
    operation: str
    committed: bool
    result_digest: str = ""
    error_code: str = ""


__all__ = [
    "ApprovalReceipt",
    "ApprovalRequest",
    "ApprovalResolution",
    "AuthorizationDecision",
    "AuthorizationOperation",
    "AuthorizationRequest",
    "MutationReceipt",
    "Principal",
    "stable_payload_digest",
]
