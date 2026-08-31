"""Typed request, result, and event payloads for Gateway protocol v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias, Union

from chatcopilot.contracts.gateway import (
    ChannelAccountRef,
    ConversationRef,
    DeliveryStage,
    ResourceKind,
)


ChannelConnectionState: TypeAlias = Literal[
    "starting",
    "connected",
    "disconnected",
    "degraded",
    "failed",
]
ApprovalStatus: TypeAlias = Literal["pending", "resolved", "expired"]
ApprovalDecisionValue: TypeAlias = Literal["approve", "deny"]
ApprovalDecisionOption: TypeAlias = Literal["approve", "deny"]
ChatStopReason: TypeAlias = Literal["completed", "aborted"]
ChatRunState: TypeAlias = Literal[
    "accepted",
    "running",
    "abort_requested",
    "recovery_required",
    "completed",
    "aborted",
    "failed",
]
OutboundDeliveryState: TypeAlias = Literal[
    "pending",
    "submitting",
    "provider_submitted",
    "provider_acknowledged",
    "delivery_unknown",
    "failed",
]


@dataclass(frozen=True)
class TextRpcSegment:
    text: str
    kind: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class ReplyRpcSegment:
    message_id: str
    kind: Literal["reply"] = field(default="reply", init=False)


@dataclass(frozen=True)
class ResourceRpcSegment:
    kind: ResourceKind
    resource_id: str


CanonicalRpcSegment = Union[TextRpcSegment, ReplyRpcSegment, ResourceRpcSegment]


@dataclass(frozen=True)
class ChannelSnapshot:
    account: ChannelAccountRef
    state: ChannelConnectionState
    capabilities: tuple[str, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    account: ChannelAccountRef
    conversation: ConversationRef
    mode: str
    debug: bool
    event_cursor: int
    active_run_id: str | None = None


@dataclass(frozen=True)
class ApprovalSnapshot:
    approval_id: str
    session_id: str
    operation: str
    target: str
    policy_version: str
    expires_at_ms: int
    status: ApprovalStatus
    allowed_decisions: tuple[ApprovalDecisionOption, ...]
    challenge: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class HealthParams:
    pass


@dataclass(frozen=True)
class StatusParams:
    pass


@dataclass(frozen=True)
class ChannelsListParams:
    pass


@dataclass(frozen=True)
class EventsReplayParams:
    after_seq: int = 0
    limit: int = 100


@dataclass(frozen=True)
class SessionsCreateParams:
    session_id: str | None = None
    mode: str = "default"
    debug: bool = False


@dataclass(frozen=True)
class SessionsListParams:
    cursor: int = 0
    limit: int = 50


@dataclass(frozen=True)
class SessionsGetParams:
    session_id: str


@dataclass(frozen=True)
class SessionsPatchParams:
    session_id: str
    mode: str | None = None
    debug: bool | None = None


@dataclass(frozen=True)
class ChatSendParams:
    session_id: str
    segments: tuple[CanonicalRpcSegment, ...]
    message_id: str | None = None


@dataclass(frozen=True)
class ChatAbortParams:
    session_id: str
    run_id: str


@dataclass(frozen=True)
class RunsGetParams:
    session_id: str
    run_id: str


@dataclass(frozen=True)
class RunsLatestParams:
    session_id: str


@dataclass(frozen=True)
class DeliveriesGetParams:
    session_id: str
    run_id: str | None = None
    outbound_id: str | None = None


@dataclass(frozen=True)
class ApprovalsListParams:
    session_id: str | None = None
    cursor: int = 0
    limit: int = 50


@dataclass(frozen=True)
class ApprovalsResolveParams:
    approval_id: str
    decision: ApprovalDecisionValue
    challenge: str


GatewayRequestParams = Union[
    HealthParams,
    StatusParams,
    ChannelsListParams,
    EventsReplayParams,
    SessionsCreateParams,
    SessionsListParams,
    SessionsGetParams,
    SessionsPatchParams,
    ChatSendParams,
    ChatAbortParams,
    RunsGetParams,
    RunsLatestParams,
    DeliveriesGetParams,
    ApprovalsListParams,
    ApprovalsResolveParams,
]


@dataclass(frozen=True)
class HealthResult:
    ready: bool
    server_generation: int
    event_cursor: int


@dataclass(frozen=True)
class StatusResult:
    ready: bool
    server_generation: int
    event_cursor: int
    active_runs: int
    session_count: int


@dataclass(frozen=True)
class ChannelsListResult:
    channels: tuple[ChannelSnapshot, ...]


@dataclass(frozen=True)
class SessionsCreateResult:
    session: SessionSnapshot


@dataclass(frozen=True)
class SessionsListResult:
    sessions: tuple[SessionSnapshot, ...]
    next_cursor: int | None = None


@dataclass(frozen=True)
class SessionsGetResult:
    session: SessionSnapshot


@dataclass(frozen=True)
class SessionsPatchResult:
    session: SessionSnapshot


@dataclass(frozen=True)
class ChatSendResult:
    session_id: str
    run_id: str
    status: Literal["accepted"] = "accepted"


@dataclass(frozen=True)
class ChatAbortResult:
    session_id: str
    run_id: str
    aborted: bool


@dataclass(frozen=True)
class RunSnapshot:
    session_id: str
    run_id: str
    state: ChatRunState
    segments: tuple[CanonicalRpcSegment, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class RunsGetResult:
    run: RunSnapshot


@dataclass(frozen=True)
class RunsLatestResult:
    run: RunSnapshot | None


@dataclass(frozen=True)
class DeliveryReceiptSnapshot:
    receipt_id: str
    stage: DeliveryStage
    observed_at_ms: int
    provider_message_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class DeliverySnapshot:
    outbound_id: str
    session_id: str
    run_id: str
    state: OutboundDeliveryState
    receipts: tuple[DeliveryReceiptSnapshot, ...]
    provider_message_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class DeliveriesGetResult:
    deliveries: tuple[DeliverySnapshot, ...]


@dataclass(frozen=True)
class ApprovalsListResult:
    approvals: tuple[ApprovalSnapshot, ...]
    next_cursor: int | None = None


@dataclass(frozen=True)
class ApprovalsResolveResult:
    approval_id: str
    resolved: bool
    accepted: bool
    code: str


@dataclass(frozen=True)
class ChannelStatusEvent:
    channel: ChannelSnapshot


@dataclass(frozen=True)
class SessionUpdatedEvent:
    session: SessionSnapshot


@dataclass(frozen=True)
class ChatUpdateEvent:
    session_id: str
    run_id: str
    text: str


@dataclass(frozen=True)
class ChatFinalEvent:
    session_id: str
    run_id: str
    stop_reason: ChatStopReason
    segments: tuple[CanonicalRpcSegment, ...] = ()


@dataclass(frozen=True)
class ChatErrorEvent:
    session_id: str
    run_id: str
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class ApprovalRequestedEvent:
    approval: ApprovalSnapshot


@dataclass(frozen=True)
class DeliveryUpdatedEvent:
    outbound_id: str
    receipt_id: str
    stage: DeliveryStage
    observed_at_ms: int
    session_id: str | None = None
    run_id: str | None = None
    message_id: str | None = None
    provider_message_id: str | None = None
    error_code: str | None = None


GatewayEventPayload = Union[
    ChannelStatusEvent,
    SessionUpdatedEvent,
    ChatUpdateEvent,
    ChatFinalEvent,
    ChatErrorEvent,
    ApprovalRequestedEvent,
    DeliveryUpdatedEvent,
]


@dataclass(frozen=True)
class GatewayReplayItem:
    event: str
    seq: int
    payload: GatewayEventPayload


@dataclass(frozen=True)
class EventsReplayResult:
    events: tuple[GatewayReplayItem, ...]
    next_cursor: int
    current_cursor: int
    resync_required: bool


GatewayMethodResult = Union[
    HealthResult,
    StatusResult,
    ChannelsListResult,
    EventsReplayResult,
    SessionsCreateResult,
    SessionsListResult,
    SessionsGetResult,
    SessionsPatchResult,
    ChatSendResult,
    ChatAbortResult,
    RunsGetResult,
    RunsLatestResult,
    DeliveriesGetResult,
    ApprovalsListResult,
    ApprovalsResolveResult,
]


__all__ = [
    "ApprovalDecisionOption",
    "ApprovalDecisionValue",
    "ApprovalRequestedEvent",
    "ApprovalSnapshot",
    "ApprovalStatus",
    "ApprovalsListParams",
    "ApprovalsListResult",
    "ApprovalsResolveParams",
    "ApprovalsResolveResult",
    "CanonicalRpcSegment",
    "ChannelConnectionState",
    "ChannelSnapshot",
    "ChannelStatusEvent",
    "ChannelsListParams",
    "ChannelsListResult",
    "ChatAbortParams",
    "ChatAbortResult",
    "ChatErrorEvent",
    "ChatFinalEvent",
    "ChatSendParams",
    "ChatSendResult",
    "ChatRunState",
    "ChatStopReason",
    "ChatUpdateEvent",
    "DeliveriesGetParams",
    "DeliveriesGetResult",
    "DeliveryReceiptSnapshot",
    "DeliverySnapshot",
    "DeliveryUpdatedEvent",
    "EventsReplayParams",
    "EventsReplayResult",
    "GatewayEventPayload",
    "GatewayMethodResult",
    "GatewayReplayItem",
    "GatewayRequestParams",
    "HealthParams",
    "HealthResult",
    "OutboundDeliveryState",
    "ReplyRpcSegment",
    "ResourceRpcSegment",
    "RunSnapshot",
    "RunsGetParams",
    "RunsGetResult",
    "RunsLatestParams",
    "RunsLatestResult",
    "SessionSnapshot",
    "SessionUpdatedEvent",
    "SessionsCreateParams",
    "SessionsCreateResult",
    "SessionsGetParams",
    "SessionsGetResult",
    "SessionsListParams",
    "SessionsListResult",
    "SessionsPatchParams",
    "SessionsPatchResult",
    "StatusParams",
    "StatusResult",
    "TextRpcSegment",
]
