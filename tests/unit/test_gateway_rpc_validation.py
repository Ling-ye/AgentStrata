from __future__ import annotations

from dataclasses import replace

import pytest

from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.gateway_rpc import (
    ApprovalRequestedEvent,
    ApprovalSnapshot,
    ApprovalsListParams,
    ApprovalsListResult,
    ApprovalsResolveParams,
    ApprovalsResolveResult,
    ChannelSnapshot,
    ChannelStatusEvent,
    ChannelsListParams,
    ChannelsListResult,
    ChatAbortParams,
    ChatAbortResult,
    ChatErrorEvent,
    ChatFinalEvent,
    ChatSendParams,
    ChatSendResult,
    ChatUpdateEvent,
    DeliveriesGetParams,
    DeliveriesGetResult,
    DeliveryReceiptSnapshot,
    DeliverySnapshot,
    DeliveryUpdatedEvent,
    HealthParams,
    HealthResult,
    ReplyRpcSegment,
    ResourceRpcSegment,
    RunSnapshot,
    RunsGetParams,
    RunsGetResult,
    RunsLatestParams,
    RunsLatestResult,
    SessionSnapshot,
    SessionUpdatedEvent,
    SessionsCreateParams,
    SessionsCreateResult,
    SessionsGetParams,
    SessionsGetResult,
    SessionsListParams,
    SessionsListResult,
    SessionsPatchParams,
    SessionsPatchResult,
    StatusParams,
    StatusResult,
    TextRpcSegment,
)
from chatcopilot.gateway.protocol import GatewayProtocolError
from chatcopilot.gateway.rpc_validation import (
    MAX_RPC_BYTES,
    MAX_RPC_JSON_DEPTH,
    MAX_RPC_SEGMENTS,
    MAX_RPC_TEXT_CHARS,
    parse_event_payload,
    parse_method_result,
    parse_request_params,
    serialize_event_payload,
    serialize_method_result,
    serialize_request_params,
)


def _session() -> SessionSnapshot:
    return SessionSnapshot(
        session_id="session-1",
        account=ChannelAccountRef("qq", "bot-account"),
        conversation=ConversationRef("group", "conversation-1"),
        mode="general",
        debug=False,
        event_cursor=8,
        active_run_id="run-1",
    )


def _channel() -> ChannelSnapshot:
    return ChannelSnapshot(
        account=ChannelAccountRef("qq", "bot-account"),
        state="connected",
        capabilities=("text", "file"),
    )


def _approval() -> ApprovalSnapshot:
    return ApprovalSnapshot(
        approval_id="approval-1",
        session_id="session-1",
        run_id="run-1",
        operation="tool.execute",
        target="workspace mutation",
        policy_version="policy-v1",
        expires_at_ms=2_000_000,
        status="pending",
        allowed_decisions=("approve", "deny"),
        challenge="confirm-action",
    )


def _delivery() -> DeliverySnapshot:
    return DeliverySnapshot(
        outbound_id="outbound-1",
        session_id="session-1",
        run_id="run-1",
        state="delivery_unknown",
        receipts=(
            DeliveryReceiptSnapshot(
                receipt_id="receipt-accepted",
                stage="gateway_accepted",
                observed_at_ms=1_000,
            ),
            DeliveryReceiptSnapshot(
                receipt_id="receipt-unknown",
                stage="delivery_unknown",
                observed_at_ms=2_000,
                error_code="gateway_restarted_before_ack",
            ),
        ),
        error_code="gateway_restarted_before_ack",
    )


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("health", HealthParams()),
        ("status", StatusParams()),
        ("channels.list", ChannelsListParams()),
        ("sessions.create", SessionsCreateParams("session-1", "general", True)),
        ("sessions.list", SessionsListParams(cursor=4, limit=20)),
        ("sessions.get", SessionsGetParams("session-1")),
        ("sessions.patch", SessionsPatchParams("session-1", mode="general")),
        (
            "chat.send",
            ChatSendParams(
                session_id="session-1",
                message_id="message-1",
                segments=(
                    TextRpcSegment("hello"),
                    ReplyRpcSegment("message-0"),
                    ResourceRpcSegment("image", "resource-1"),
                ),
            ),
        ),
        ("chat.abort", ChatAbortParams("session-1", "run-1")),
        ("runs.get", RunsGetParams("session-1", "run-1")),
        ("runs.latest", RunsLatestParams("session-1")),
        ("deliveries.get", DeliveriesGetParams("session-1", run_id="run-1")),
        (
            "deliveries.get",
            DeliveriesGetParams("session-1", outbound_id="outbound-1"),
        ),
        ("approvals.list", ApprovalsListParams("session-1", cursor=2, limit=10)),
        (
            "approvals.resolve",
            ApprovalsResolveParams("approval-1", "approve", "confirm-action"),
        ),
    ],
)
def test_request_dtos_round_trip_with_camel_case(method: str, params: object) -> None:
    wire = serialize_request_params(method, params)  # type: ignore[arg-type]

    assert parse_request_params(method, wire) == params
    assert all("_" not in key for key in wire)


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("health", HealthResult(True, 3, 8)),
        ("status", StatusResult(True, 3, 8, 1, 2)),
        ("channels.list", ChannelsListResult((_channel(),))),
        ("sessions.create", SessionsCreateResult(_session())),
        ("sessions.list", SessionsListResult((_session(),), next_cursor=10)),
        ("sessions.get", SessionsGetResult(_session())),
        ("sessions.patch", SessionsPatchResult(_session())),
        ("chat.send", ChatSendResult("session-1", "run-1")),
        ("chat.abort", ChatAbortResult("session-1", "run-1", True)),
        (
            "runs.get",
            RunsGetResult(
                RunSnapshot(
                    "session-1",
                    "run-1",
                    "completed",
                    segments=(TextRpcSegment("done"),),
                )
            ),
        ),
        (
            "runs.latest",
            RunsLatestResult(
                RunSnapshot(
                    "session-1",
                    "run-1",
                    "completed",
                    segments=(TextRpcSegment("done"),),
                )
            ),
        ),
        ("runs.latest", RunsLatestResult(None)),
        ("deliveries.get", DeliveriesGetResult((_delivery(),))),
        ("approvals.list", ApprovalsListResult((_approval(),), next_cursor=2)),
        (
            "approvals.resolve",
            ApprovalsResolveResult("approval-1", True, True, "approval-accepted"),
        ),
    ],
)
def test_method_result_dtos_round_trip(method: str, result: object) -> None:
    wire = serialize_method_result(method, result)  # type: ignore[arg-type]

    assert parse_method_result(method, wire) == result


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("channel.status", ChannelStatusEvent(_channel())),
        ("session.updated", SessionUpdatedEvent(_session())),
        ("chat.update", ChatUpdateEvent("session-1", "run-1", "delta")),
        (
            "chat.final",
            ChatFinalEvent(
                "session-1",
                "run-1",
                "completed",
                (TextRpcSegment("done"),),
            ),
        ),
        (
            "chat.error",
            ChatErrorEvent("session-1", "run-1", "model_failed", "failed", False),
        ),
        ("approval.requested", ApprovalRequestedEvent(_approval())),
        (
            "delivery.updated",
            DeliveryUpdatedEvent(
                outbound_id="outbound-1",
                receipt_id="receipt-1",
                stage="provider_acknowledged",
                observed_at_ms=1_000,
                session_id="session-1",
                run_id="run-1",
                message_id="message-1",
                provider_message_id="provider-message-1",
            ),
        ),
    ],
)
def test_event_dtos_round_trip(event: str, payload: object) -> None:
    wire = serialize_event_payload(event, payload)  # type: ignore[arg-type]

    assert parse_event_payload(event, wire) == payload


def test_unknown_method_event_and_fields_fail_closed() -> None:
    with pytest.raises(GatewayProtocolError) as caught:
        parse_request_params("host.shell", {})
    assert caught.value.code == "unknown_method"

    with pytest.raises(GatewayProtocolError) as caught:
        parse_event_payload("host.changed", {})
    assert caught.value.code == "unknown_event"

    with pytest.raises(GatewayProtocolError, match="fields are invalid"):
        parse_request_params("health", {"verbose": True})
    with pytest.raises(GatewayProtocolError, match="fields are invalid"):
        parse_event_payload(
            "chat.update",
            {
                "sessionId": "session-1",
                "runId": "run-1",
                "text": "delta",
                "rawProviderData": {},
            },
        )


def test_boolean_and_integer_fields_are_not_interchangeable() -> None:
    with pytest.raises(GatewayProtocolError, match="must be an integer"):
        parse_request_params("sessions.list", {"cursor": True})
    with pytest.raises(GatewayProtocolError, match="must be a boolean"):
        parse_request_params("sessions.create", {"debug": 1})
    with pytest.raises(GatewayProtocolError, match="must be a boolean"):
        parse_method_result(
            "health",
            {"ready": 1, "serverGeneration": 1, "eventCursor": 0},
        )


@pytest.mark.parametrize(
    "forged",
    [
        {"role": "owner"},
        {"senderId": "sender-1"},
        {"owner": True},
        {"channel": "qq", "accountId": "bot-account"},
        {"platform": "qq", "transportEvidence": {}},
    ],
)
def test_acp_session_creation_cannot_assert_actor_or_transport_identity(
    forged: dict[str, object],
) -> None:
    with pytest.raises(GatewayProtocolError) as caught:
        parse_request_params("sessions.create", forged)
    assert caught.value.code == "untrusted_identity"


def test_nested_identity_forgery_is_rejected_before_segment_processing() -> None:
    with pytest.raises(GatewayProtocolError) as caught:
        parse_request_params(
            "chat.send",
            {
                "sessionId": "session-1",
                "segments": [{"kind": "text", "text": "hello", "actorRef": "actor-1"}],
            },
        )
    assert caught.value.code == "untrusted_identity"


@pytest.mark.parametrize(
    "segment",
    [
        {"kind": "text", "text": "hello", "resourceId": "resource-1"},
        {"kind": "image", "resourceId": "resource-1", "text": "mixed"},
        {"kind": "reply", "resourceId": "resource-1"},
        {"kind": "mention", "target": "sender-1"},
        {"kind": "unknown"},
    ],
)
def test_chat_segments_reject_mixed_or_unrecognized_shapes(
    segment: dict[str, object],
) -> None:
    with pytest.raises(GatewayProtocolError):
        parse_request_params(
            "chat.send",
            {"sessionId": "session-1", "segments": [segment]},
        )


@pytest.mark.parametrize(
    "resource_id",
    ["../resource", "https://example.invalid/resource", "file:resource"],
)
def test_chat_resource_segments_reject_paths_and_urls(resource_id: str) -> None:
    with pytest.raises(GatewayProtocolError):
        parse_request_params(
            "chat.send",
            {
                "sessionId": "session-1",
                "segments": [{"kind": "file", "resourceId": resource_id}],
            },
        )


def test_chat_abort_requires_exact_session_and_run_binding() -> None:
    assert parse_request_params(
        "chat.abort", {"sessionId": "session-1", "runId": "run-1"}
    ) == ChatAbortParams("session-1", "run-1")

    with pytest.raises(GatewayProtocolError, match="fields are invalid"):
        parse_request_params("chat.abort", {"runId": "run-1"})
    with pytest.raises(GatewayProtocolError, match="fields are invalid"):
        parse_request_params("chat.abort", {"sessionId": "session-1"})


def test_run_snapshot_terminal_shape_is_fail_closed() -> None:
    with pytest.raises(GatewayProtocolError, match="requires only an error code"):
        parse_method_result(
            "runs.get",
            {
                "run": {
                    "sessionId": "session-1",
                    "runId": "run-1",
                    "state": "failed",
                    "segments": [],
                }
            },
        )
    with pytest.raises(GatewayProtocolError, match="non-terminal"):
        parse_method_result(
            "runs.get",
            {
                "run": {
                    "sessionId": "session-1",
                    "runId": "run-1",
                    "state": "running",
                    "segments": [{"kind": "text", "text": "not terminal"}],
                }
            },
        )


@pytest.mark.parametrize(
    "params",
    [
        {"sessionId": "session-1"},
        {
            "sessionId": "session-1",
            "runId": "run-1",
            "outboundId": "outbound-1",
        },
    ],
)
def test_delivery_query_requires_exactly_one_durable_locator(
    params: dict[str, str],
) -> None:
    with pytest.raises(GatewayProtocolError, match="exactly one"):
        parse_request_params("deliveries.get", params)


def test_delivery_snapshot_requires_receipt_to_prove_current_state() -> None:
    wire = serialize_method_result(
        "deliveries.get",
        DeliveriesGetResult((_delivery(),)),
    )
    wire["deliveries"][0]["state"] = "failed"  # type: ignore[index]
    with pytest.raises(GatewayProtocolError, match="prove the current outbound state"):
        parse_method_result("deliveries.get", wire)


def test_delivery_run_reference_requires_a_session_reference() -> None:
    with pytest.raises(GatewayProtocolError, match="bound to sessionId"):
        parse_event_payload(
            "delivery.updated",
            {
                "outboundId": "outbound-1",
                "receiptId": "receipt-1",
                "stage": "provider_submitted",
                "observedAtMs": 1,
                "runId": "run-1",
            },
        )


def test_approval_snapshot_exposes_challenge_only_while_pending() -> None:
    terminal = {
        "approvalId": "approval-1",
        "sessionId": "session-1",
        "operation": "conversation.reset",
        "target": "current-conversation",
        "policyVersion": "policy-v1",
        "expiresAtMs": 2_000_000,
        "status": "resolved",
        "allowedDecisions": [],
    }
    parsed = parse_method_result(
        "approvals.list",
        {"approvals": [terminal]},
    )
    assert parsed.approvals[0].challenge is None  # type: ignore[union-attr]

    with pytest.raises(GatewayProtocolError, match="pending approval requires"):
        parse_method_result(
            "approvals.list",
            {
                "approvals": [
                    {
                        **terminal,
                        "status": "pending",
                        "allowedDecisions": ["approve", "deny"],
                    }
                ]
            },
        )
    with pytest.raises(GatewayProtocolError, match="terminal approval cannot expose"):
        parse_method_result(
            "approvals.list",
            {
                "approvals": [
                    {
                        **terminal,
                        "allowedDecisions": ["approve", "deny"],
                        "challenge": _approval().challenge,
                    }
                ]
            },
        )


def test_patch_requires_a_real_change_and_serializer_revalidates_dtos() -> None:
    with pytest.raises(GatewayProtocolError, match="requires mode or debug"):
        parse_request_params("sessions.patch", {"sessionId": "session-1"})
    with pytest.raises(GatewayProtocolError, match="requires mode or debug"):
        serialize_request_params("sessions.patch", SessionsPatchParams("session-1"))
    with pytest.raises(GatewayProtocolError):
        serialize_request_params(
            "chat.send",
            ChatSendParams(
                "session-1",
                (ResourceRpcSegment("file", "../resource"),),
            ),
        )


def test_rpc_depth_collection_text_and_total_bytes_are_bounded() -> None:
    nested: object = "leaf"
    for _ in range(MAX_RPC_JSON_DEPTH + 2):
        nested = {"nested": nested}
    with pytest.raises(GatewayProtocolError, match="nesting is too deep"):
        parse_request_params("health", nested)  # type: ignore[arg-type]

    with pytest.raises(GatewayProtocolError, match="bounded array"):
        parse_request_params(
            "chat.send",
            {
                "sessionId": "session-1",
                "segments": [{"kind": "text", "text": "x"} for _ in range(MAX_RPC_SEGMENTS + 1)],
            },
        )

    with pytest.raises(GatewayProtocolError, match="string is too large"):
        parse_request_params(
            "chat.send",
            {
                "sessionId": "session-1",
                "segments": [{"kind": "text", "text": "x" * (MAX_RPC_TEXT_CHARS + 1)}],
            },
        )

    piece = "x" * (MAX_RPC_TEXT_CHARS - 1)
    oversized = {
        "sessionId": "session-1",
        "segments": [{"kind": "text", "text": piece} for _ in range(5)],
    }
    assert len(str(oversized).encode()) > MAX_RPC_BYTES
    with pytest.raises(GatewayProtocolError, match="byte limit"):
        parse_request_params("chat.send", oversized)


def test_result_and_event_serializers_reject_wrong_dto_or_invalid_nested_values() -> None:
    with pytest.raises(GatewayProtocolError, match="wrong typed DTO"):
        serialize_method_result("health", StatusResult(True, 1, 0, 0, 0))
    with pytest.raises(GatewayProtocolError):
        serialize_event_payload(
            "session.updated",
            SessionUpdatedEvent(replace(_session(), event_cursor=-1)),
        )
