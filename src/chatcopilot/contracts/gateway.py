"""Platform-neutral transport contracts shared by Channels and the Gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


MessageSegmentKind = Literal[
    "text",
    "mention",
    "reply",
    "image",
    "audio",
    "video",
    "file",
    "unknown",
]
ResourceKind = Literal["image", "audio", "video", "file"]
DeliveryStage = Literal[
    "gateway_accepted",
    "provider_submitted",
    "provider_acknowledged",
    "delivery_unknown",
    "platform_displayed",
    "user_read",
    "failed",
]


@dataclass(frozen=True)
class ChannelAccountRef:
    """Stable Channel account identity; provider implementation is deliberately absent."""

    channel: str
    account_id: str


@dataclass(frozen=True)
class ConversationRef:
    """Stable platform conversation independent from the current sender."""

    kind: str
    conversation_id: str


@dataclass(frozen=True)
class SenderClaim:
    """Sender asserted by an authenticated Channel, before host authorization."""

    sender_id: str
    display_name: str | None = None


@dataclass(frozen=True)
class MessageSegment:
    """One normalized message element without a native provider frame."""

    kind: MessageSegmentKind
    text: str | None = None
    target: str | None = None
    resource_ticket_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransportEvidence:
    """Authenticated native-event facts that authorization may independently verify."""

    account: ChannelAccountRef
    conversation: ConversationRef
    sender: SenderClaim
    event_id: str
    message_id: str | None
    connection_generation: str
    frame_sha256: str
    observed_at: float


@dataclass(frozen=True)
class ResourceTicket:
    """Event-bound resource locator that may be materialized only after admission.

    ``provider_ref`` is opaque Channel state used to recover the resource after a
    restart. It is untrusted data and never participates in identity or permission
    decisions.
    """

    ticket_id: str
    account: ChannelAccountRef
    conversation: ConversationRef
    sender_id: str
    event_id: str
    message_id: str | None
    kind: ResourceKind
    name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    expires_at: float | None = None
    provider_ref: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalInboundEvent:
    """Canonical inbound event durable before authorization or Agent execution."""

    evidence: TransportEvidence
    segments: tuple[MessageSegment, ...]
    resource_tickets: tuple[ResourceTicket, ...] = ()


@dataclass(frozen=True)
class OutboundEnvelope:
    """Provider-neutral outbound message durable before Channel submission."""

    outbound_id: str
    account: ChannelAccountRef
    conversation: ConversationRef
    segments: tuple[MessageSegment, ...]
    created_at: float
    session_id: str | None = None
    run_id: str | None = None
    reply_to_message_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    """Evidence for exactly one observed outbound delivery boundary."""

    receipt_id: str
    outbound_id: str
    stage: DeliveryStage
    observed_at: float
    provider_message_id: str | None = None
    error_code: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "CanonicalInboundEvent",
    "ChannelAccountRef",
    "ConversationRef",
    "DeliveryReceipt",
    "DeliveryStage",
    "MessageSegment",
    "MessageSegmentKind",
    "OutboundEnvelope",
    "ResourceKind",
    "ResourceTicket",
    "SenderClaim",
    "TransportEvidence",
]
