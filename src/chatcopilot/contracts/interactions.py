"""Host-owned interactions; responders never inherit the requester's authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol

from chatcopilot.contracts.model_runtime import freeze_json, json_value


@dataclass(frozen=True)
class ActorResponder:
    actor_ref: str
    transport_evidence_ref: str

    def to_payload(self):
        return {
            "kind": "actor",
            "actor_ref": self.actor_ref,
            "transport_evidence_ref": self.transport_evidence_ref,
        }


@dataclass(frozen=True)
class OperatorResponder:
    client_id: str
    credential_binding_ref: str

    def to_payload(self):
        return {
            "kind": "operator",
            "client_id": self.client_id,
            "credential_binding_ref": self.credential_binding_ref,
        }


@dataclass(frozen=True)
class InteractionSnapshot:
    interaction_id: str
    kind: Literal["approval", "user_input", "mcp_elicitation"]
    session_id: str
    execution_id: str
    requester_actor_ref: str
    payload: Mapping[str, Any]
    payload_digest: str
    policy_revision: str
    created_at: float
    expires_at: float
    state: str = "pending"

    def __post_init__(self):
        if self.kind not in {"approval", "user_input", "mcp_elicitation"}:
            raise ValueError("unknown interaction kind")
        object.__setattr__(self, "payload", freeze_json(self.payload))

    def to_payload(self):
        return {
            "interactionId": self.interaction_id,
            "kind": self.kind,
            "sessionId": self.session_id,
            "executionId": self.execution_id,
            "requesterActorRef": self.requester_actor_ref,
            "payload": json_value(self.payload),
            "payloadDigest": self.payload_digest,
            "policyRevision": self.policy_revision,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "state": self.state,
        }


class InteractionPort(Protocol):
    def __call__(self, method: str, params: dict, task: Any, cancellation: Any) -> dict: ...
