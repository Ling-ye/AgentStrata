"""Closed-schema validation for typed Gateway v1 method and event payloads."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
from typing import Any, TypeVar, cast

from chatcopilot.contracts.gateway import (
    ChannelAccountRef,
    ConversationRef,
    DeliveryStage,
    ResourceKind,
)
from chatcopilot.contracts.gateway_rpc import (
    ApprovalDecisionOption,
    ApprovalDecisionValue,
    ApprovalRequestedEvent,
    ApprovalSnapshot,
    ApprovalStatus,
    ApprovalsListParams,
    ApprovalsListResult,
    ApprovalsResolveParams,
    ApprovalsResolveResult,
    CanonicalRpcSegment,
    ChannelConnectionState,
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
    ChatRunState,
    ChatStopReason,
    ChatUpdateEvent,
    DeliveriesGetParams,
    DeliveriesGetResult,
    DeliveryReceiptSnapshot,
    DeliverySnapshot,
    DeliveryUpdatedEvent,
    EventsReplayParams,
    EventsReplayResult,
    GatewayEventPayload,
    GatewayMethodResult,
    GatewayReplayItem,
    GatewayRequestParams,
    HealthParams,
    HealthResult,
    OutboundDeliveryState,
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
from chatcopilot.gateway.protocol import (
    GATEWAY_EVENTS,
    GATEWAY_METHODS,
    MUTATION_METHODS,
    GatewayProtocolError,
)


MAX_RPC_BYTES = 256 * 1024
MAX_RPC_JSON_DEPTH = 16
MAX_RPC_COLLECTION_ITEMS = 256
MAX_RPC_TOTAL_ITEMS = 4096
MAX_RPC_TEXT_CHARS = 64 * 1024
MAX_RPC_SEGMENTS = 64

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPAQUE_RESOURCE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESOURCE_KINDS = frozenset({"image", "audio", "video", "file"})
_CHANNEL_STATES = frozenset({"starting", "connected", "disconnected", "degraded", "failed"})
_APPROVAL_STATUSES = frozenset({"pending", "resolved", "expired"})
_APPROVAL_DECISIONS = frozenset({"approve", "deny"})
_STOP_REASONS = frozenset({"completed", "aborted"})
_RUN_STATES = frozenset(
    {
        "accepted",
        "running",
        "abort_requested",
        "recovery_required",
        "completed",
        "aborted",
        "failed",
    }
)
_DELIVERY_STAGES = frozenset(
    {
        "gateway_accepted",
        "provider_submitted",
        "provider_acknowledged",
        "delivery_unknown",
        "platform_displayed",
        "user_read",
        "failed",
    }
)
_OUTBOUND_STATES = frozenset(
    {
        "pending",
        "submitting",
        "provider_submitted",
        "provider_acknowledged",
        "delivery_unknown",
        "failed",
    }
)
_FORBIDDEN_MUTATION_IDENTITY_KEYS = frozenset(
    {
        "accountid",
        "actor",
        "actorid",
        "actorref",
        "attestation",
        "channel",
        "conversation",
        "conversationid",
        "groupid",
        "owner",
        "ownerid",
        "platform",
        "principal",
        "qq",
        "qquserid",
        "role",
        "sender",
        "senderid",
        "transportevidence",
        "userid",
    }
)

_T = TypeVar("_T")
_RequestParser = Callable[[Mapping[str, Any]], GatewayRequestParams]
_ResultParser = Callable[[Mapping[str, Any]], GatewayMethodResult]
_EventParser = Callable[[Mapping[str, Any]], GatewayEventPayload]


def parse_request_params(method: str, params: Mapping[str, Any]) -> GatewayRequestParams:
    """Parse one public method payload before invoking a Gateway handler."""

    parser = _REQUEST_PARSERS.get(method)
    if parser is None:
        raise GatewayProtocolError("unknown_method", "Gateway method is not recognized")
    payload = _root_object(params, f"{method} params")
    if method in MUTATION_METHODS:
        _reject_mutation_identity_claims(payload)
    return parser(payload)


def serialize_request_params(
    method: str,
    params: GatewayRequestParams,
) -> dict[str, Any]:
    """Serialize one typed request using the protocol's camelCase wire names."""

    serializer = _REQUEST_SERIALIZERS.get(method)
    if serializer is None:
        raise GatewayProtocolError("unknown_method", "Gateway method is not recognized")
    payload = serializer(params)
    parse_request_params(method, payload)
    return payload


def parse_method_result(method: str, result: Mapping[str, Any]) -> GatewayMethodResult:
    parser = _RESULT_PARSERS.get(method)
    if parser is None:
        raise GatewayProtocolError("unknown_method", "Gateway method is not recognized")
    return parser(_root_object(result, f"{method} result"))


def serialize_method_result(
    method: str,
    result: GatewayMethodResult,
) -> dict[str, Any]:
    serializer = _RESULT_SERIALIZERS.get(method)
    if serializer is None:
        raise GatewayProtocolError("unknown_method", "Gateway method is not recognized")
    payload = serializer(result)
    parse_method_result(method, payload)
    return payload


def parse_event_payload(event: str, payload: Mapping[str, Any]) -> GatewayEventPayload:
    parser = _EVENT_PARSERS.get(event)
    if parser is None:
        raise GatewayProtocolError("unknown_event", "Gateway event is not recognized")
    return parser(_root_object(payload, f"{event} payload"))


def serialize_event_payload(
    event: str,
    payload: GatewayEventPayload,
) -> dict[str, Any]:
    serializer = _EVENT_SERIALIZERS.get(event)
    if serializer is None:
        raise GatewayProtocolError("unknown_event", "Gateway event is not recognized")
    encoded = serializer(payload)
    parse_event_payload(event, encoded)
    return encoded


def _parse_health(params: Mapping[str, Any]) -> HealthParams:
    _exact_keys(params, required=set(), label="health params")
    return HealthParams()


def _parse_status(params: Mapping[str, Any]) -> StatusParams:
    _exact_keys(params, required=set(), label="status params")
    return StatusParams()


def _parse_channels_list(params: Mapping[str, Any]) -> ChannelsListParams:
    _exact_keys(params, required=set(), label="channels.list params")
    return ChannelsListParams()


def _parse_events_replay(params: Mapping[str, Any]) -> EventsReplayParams:
    _exact_keys(
        params,
        required={"afterSeq"},
        optional={"limit"},
        label="events.replay params",
    )
    return EventsReplayParams(
        after_seq=_required_int(params["afterSeq"], "afterSeq", 0),
        limit=_optional_int(params, "limit", default=100, minimum=1, maximum=256),
    )


def _parse_sessions_create(params: Mapping[str, Any]) -> SessionsCreateParams:
    _exact_keys(
        params,
        required=set(),
        optional={"sessionId", "mode", "debug"},
        label="sessions.create params",
    )
    return SessionsCreateParams(
        session_id=_optional_id(params, "sessionId"),
        mode=_optional_id(params, "mode") or "default",
        debug=_optional_bool(params, "debug", default=False),
    )


def _parse_sessions_list(params: Mapping[str, Any]) -> SessionsListParams:
    _exact_keys(
        params,
        required=set(),
        optional={"cursor", "limit"},
        label="sessions.list params",
    )
    return SessionsListParams(
        cursor=_optional_int(params, "cursor", default=0, minimum=0),
        limit=_optional_int(params, "limit", default=50, minimum=1, maximum=100),
    )


def _parse_sessions_get(params: Mapping[str, Any]) -> SessionsGetParams:
    _exact_keys(params, required={"sessionId"}, label="sessions.get params")
    return SessionsGetParams(session_id=_required_id(params["sessionId"], "sessionId"))


def _parse_sessions_patch(params: Mapping[str, Any]) -> SessionsPatchParams:
    _exact_keys(
        params,
        required={"sessionId"},
        optional={"mode", "debug"},
        label="sessions.patch params",
    )
    if "mode" not in params and "debug" not in params:
        raise GatewayProtocolError(
            "invalid_rpc",
            "sessions.patch requires mode or debug",
        )
    return SessionsPatchParams(
        session_id=_required_id(params["sessionId"], "sessionId"),
        mode=_optional_id(params, "mode"),
        debug=_optional_bool(params, "debug") if "debug" in params else None,
    )


def _parse_chat_send(params: Mapping[str, Any]) -> ChatSendParams:
    _exact_keys(
        params,
        required={"sessionId", "segments"},
        optional={"messageId"},
        label="chat.send params",
    )
    return ChatSendParams(
        session_id=_required_id(params["sessionId"], "sessionId"),
        segments=_parse_segments(params["segments"], minimum=1),
        message_id=_optional_id(params, "messageId"),
    )


def _parse_chat_abort(params: Mapping[str, Any]) -> ChatAbortParams:
    _exact_keys(
        params,
        required={"sessionId", "runId"},
        label="chat.abort params",
    )
    return ChatAbortParams(
        session_id=_required_id(params["sessionId"], "sessionId"),
        run_id=_required_id(params["runId"], "runId"),
    )


def _parse_runs_get(params: Mapping[str, Any]) -> RunsGetParams:
    _exact_keys(
        params,
        required={"sessionId", "runId"},
        label="runs.get params",
    )
    return RunsGetParams(
        session_id=_required_id(params["sessionId"], "sessionId"),
        run_id=_required_id(params["runId"], "runId"),
    )


def _parse_runs_latest(params: Mapping[str, Any]) -> RunsLatestParams:
    _exact_keys(params, required={"sessionId"}, label="runs.latest params")
    return RunsLatestParams(session_id=_required_id(params["sessionId"], "sessionId"))


def _parse_deliveries_get(params: Mapping[str, Any]) -> DeliveriesGetParams:
    _exact_keys(
        params,
        required={"sessionId"},
        optional={"runId", "outboundId"},
        label="deliveries.get params",
    )
    run_id = _optional_id(params, "runId")
    outbound_id = _optional_id(params, "outboundId")
    if (run_id is None) == (outbound_id is None):
        raise GatewayProtocolError(
            "invalid_rpc",
            "deliveries.get requires exactly one runId or outboundId",
        )
    return DeliveriesGetParams(
        session_id=_required_id(params["sessionId"], "sessionId"),
        run_id=run_id,
        outbound_id=outbound_id,
    )


def _parse_approvals_list(params: Mapping[str, Any]) -> ApprovalsListParams:
    _exact_keys(
        params,
        required=set(),
        optional={"sessionId", "cursor", "limit"},
        label="approvals.list params",
    )
    return ApprovalsListParams(
        session_id=_optional_id(params, "sessionId"),
        cursor=_optional_int(params, "cursor", default=0, minimum=0),
        limit=_optional_int(params, "limit", default=50, minimum=1, maximum=100),
    )


def _parse_approvals_resolve(params: Mapping[str, Any]) -> ApprovalsResolveParams:
    _exact_keys(
        params,
        required={"approvalId", "decision", "challenge"},
        label="approvals.resolve params",
    )
    return ApprovalsResolveParams(
        approval_id=_required_id(params["approvalId"], "approvalId"),
        decision=cast(
            ApprovalDecisionValue,
            _required_enum(params["decision"], "decision", _APPROVAL_DECISIONS),
        ),
        challenge=_required_text(params["challenge"], "challenge", max_chars=512),
    )


def _serialize_health(params: GatewayRequestParams) -> dict[str, Any]:
    _expect_type(params, HealthParams, "health params")
    return {}


def _serialize_status(params: GatewayRequestParams) -> dict[str, Any]:
    _expect_type(params, StatusParams, "status params")
    return {}


def _serialize_channels_list(params: GatewayRequestParams) -> dict[str, Any]:
    _expect_type(params, ChannelsListParams, "channels.list params")
    return {}


def _serialize_events_replay(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, EventsReplayParams, "events.replay params")
    return {"afterSeq": value.after_seq, "limit": value.limit}


def _serialize_sessions_create(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, SessionsCreateParams, "sessions.create params")
    payload: dict[str, Any] = {"mode": value.mode, "debug": value.debug}
    _put_optional(payload, "sessionId", value.session_id)
    return payload


def _serialize_sessions_list(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, SessionsListParams, "sessions.list params")
    return {"cursor": value.cursor, "limit": value.limit}


def _serialize_sessions_get(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, SessionsGetParams, "sessions.get params")
    return {"sessionId": value.session_id}


def _serialize_sessions_patch(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, SessionsPatchParams, "sessions.patch params")
    payload: dict[str, Any] = {"sessionId": value.session_id}
    _put_optional(payload, "mode", value.mode)
    if value.debug is not None:
        payload["debug"] = value.debug
    return payload


def _serialize_chat_send(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, ChatSendParams, "chat.send params")
    payload: dict[str, Any] = {
        "sessionId": value.session_id,
        "segments": [_serialize_segment(segment) for segment in value.segments],
    }
    _put_optional(payload, "messageId", value.message_id)
    return payload


def _serialize_chat_abort(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, ChatAbortParams, "chat.abort params")
    return {"sessionId": value.session_id, "runId": value.run_id}


def _serialize_runs_get(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, RunsGetParams, "runs.get params")
    return {"sessionId": value.session_id, "runId": value.run_id}


def _serialize_runs_latest(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, RunsLatestParams, "runs.latest params")
    return {"sessionId": value.session_id}


def _serialize_deliveries_get(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, DeliveriesGetParams, "deliveries.get params")
    payload: dict[str, Any] = {"sessionId": value.session_id}
    _put_optional(payload, "runId", value.run_id)
    _put_optional(payload, "outboundId", value.outbound_id)
    return payload


def _serialize_approvals_list(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, ApprovalsListParams, "approvals.list params")
    payload: dict[str, Any] = {"cursor": value.cursor, "limit": value.limit}
    _put_optional(payload, "sessionId", value.session_id)
    return payload


def _serialize_approvals_resolve(params: GatewayRequestParams) -> dict[str, Any]:
    value = _expect_type(params, ApprovalsResolveParams, "approvals.resolve params")
    return {
        "approvalId": value.approval_id,
        "decision": value.decision,
        "challenge": value.challenge,
    }


def _parse_health_result(result: Mapping[str, Any]) -> HealthResult:
    _exact_keys(
        result,
        required={"ready", "serverGeneration", "eventCursor"},
        label="health result",
    )
    return HealthResult(
        ready=_required_bool(result["ready"], "ready"),
        server_generation=_required_int(result["serverGeneration"], "serverGeneration", 1),
        event_cursor=_required_int(result["eventCursor"], "eventCursor", 0),
    )


def _parse_status_result(result: Mapping[str, Any]) -> StatusResult:
    _exact_keys(
        result,
        required={
            "ready",
            "serverGeneration",
            "eventCursor",
            "activeRuns",
            "sessionCount",
        },
        label="status result",
    )
    return StatusResult(
        ready=_required_bool(result["ready"], "ready"),
        server_generation=_required_int(result["serverGeneration"], "serverGeneration", 1),
        event_cursor=_required_int(result["eventCursor"], "eventCursor", 0),
        active_runs=_required_int(result["activeRuns"], "activeRuns", 0),
        session_count=_required_int(result["sessionCount"], "sessionCount", 0),
    )


def _parse_channels_result(result: Mapping[str, Any]) -> ChannelsListResult:
    _exact_keys(result, required={"channels"}, label="channels.list result")
    rows = _array(result["channels"], "channels", maximum=MAX_RPC_COLLECTION_ITEMS)
    return ChannelsListResult(tuple(_parse_channel_snapshot(item) for item in rows))


def _parse_events_replay_result(result: Mapping[str, Any]) -> EventsReplayResult:
    _exact_keys(
        result,
        required={"events", "nextCursor", "currentCursor", "resyncRequired"},
        label="events.replay result",
    )
    rows = _array(result["events"], "events", maximum=MAX_RPC_COLLECTION_ITEMS)
    next_cursor = _required_int(result["nextCursor"], "nextCursor", 0)
    current_cursor = _required_int(result["currentCursor"], "currentCursor", 0)
    resync_required = _required_bool(result["resyncRequired"], "resyncRequired")
    if next_cursor > current_cursor:
        raise GatewayProtocolError(
            "invalid_rpc",
            "events.replay nextCursor cannot exceed currentCursor",
        )
    events = tuple(_parse_replay_item(item) for item in rows)
    if resync_required and (events or next_cursor != current_cursor):
        raise GatewayProtocolError(
            "invalid_rpc",
            "events.replay resync must return only the current cursor",
        )
    previous = 0
    for item in events:
        if item.seq <= previous or item.seq > next_cursor:
            raise GatewayProtocolError(
                "invalid_rpc",
                "events.replay event sequence is not strictly bounded",
            )
        previous = item.seq
    return EventsReplayResult(
        events=events,
        next_cursor=next_cursor,
        current_cursor=current_cursor,
        resync_required=resync_required,
    )


def _parse_sessions_create_result(result: Mapping[str, Any]) -> SessionsCreateResult:
    _exact_keys(result, required={"session"}, label="sessions.create result")
    return SessionsCreateResult(_parse_session_snapshot(result["session"]))


def _parse_sessions_list_result(result: Mapping[str, Any]) -> SessionsListResult:
    _exact_keys(
        result,
        required={"sessions"},
        optional={"nextCursor"},
        label="sessions.list result",
    )
    rows = _array(result["sessions"], "sessions", maximum=MAX_RPC_COLLECTION_ITEMS)
    next_cursor = (
        _required_int(result["nextCursor"], "nextCursor", 0) if "nextCursor" in result else None
    )
    return SessionsListResult(
        sessions=tuple(_parse_session_snapshot(item) for item in rows),
        next_cursor=next_cursor,
    )


def _parse_sessions_get_result(result: Mapping[str, Any]) -> SessionsGetResult:
    _exact_keys(result, required={"session"}, label="sessions.get result")
    return SessionsGetResult(_parse_session_snapshot(result["session"]))


def _parse_sessions_patch_result(result: Mapping[str, Any]) -> SessionsPatchResult:
    _exact_keys(result, required={"session"}, label="sessions.patch result")
    return SessionsPatchResult(_parse_session_snapshot(result["session"]))


def _parse_chat_send_result(result: Mapping[str, Any]) -> ChatSendResult:
    _exact_keys(
        result,
        required={"sessionId", "runId", "status"},
        label="chat.send result",
    )
    if result["status"] != "accepted":
        raise GatewayProtocolError("invalid_rpc", "chat.send status is invalid")
    return ChatSendResult(
        session_id=_required_id(result["sessionId"], "sessionId"),
        run_id=_required_id(result["runId"], "runId"),
    )


def _parse_chat_abort_result(result: Mapping[str, Any]) -> ChatAbortResult:
    _exact_keys(
        result,
        required={"sessionId", "runId", "aborted"},
        label="chat.abort result",
    )
    return ChatAbortResult(
        session_id=_required_id(result["sessionId"], "sessionId"),
        run_id=_required_id(result["runId"], "runId"),
        aborted=_required_bool(result["aborted"], "aborted"),
    )


def _parse_runs_get_result(result: Mapping[str, Any]) -> RunsGetResult:
    _exact_keys(result, required={"run"}, label="runs.get result")
    return RunsGetResult(_parse_run_snapshot(result["run"]))


def _parse_runs_latest_result(result: Mapping[str, Any]) -> RunsLatestResult:
    _exact_keys(result, required={"run"}, label="runs.latest result")
    raw = result["run"]
    return RunsLatestResult(None if raw is None else _parse_run_snapshot(raw))


def _parse_deliveries_get_result(result: Mapping[str, Any]) -> DeliveriesGetResult:
    _exact_keys(result, required={"deliveries"}, label="deliveries.get result")
    rows = _array(result["deliveries"], "deliveries", minimum=1, maximum=100)
    return DeliveriesGetResult(tuple(_parse_delivery_snapshot(item) for item in rows))


def _parse_delivery_snapshot(value: Any) -> DeliverySnapshot:
    raw = _object(value, "delivery snapshot")
    _exact_keys(
        raw,
        required={
            "outboundId",
            "sessionId",
            "runId",
            "state",
            "receipts",
        },
        optional={"providerMessageId", "errorCode"},
        label="delivery snapshot",
    )
    state = cast(
        OutboundDeliveryState,
        _required_enum(raw["state"], "delivery state", _OUTBOUND_STATES),
    )
    receipts = tuple(
        _parse_delivery_receipt_snapshot(item)
        for item in _array(raw["receipts"], "delivery receipts", minimum=1, maximum=64)
    )
    expected_stage = (
        "gateway_accepted" if state in {"pending", "submitting"} else state
    )
    if receipts[-1].stage != expected_stage:
        raise GatewayProtocolError(
            "invalid_rpc",
            "delivery receipts do not prove the current outbound state",
        )
    error_code = _optional_id(raw, "errorCode")
    if state in {"delivery_unknown", "failed"}:
        if error_code is None or receipts[-1].error_code != error_code:
            raise GatewayProtocolError(
                "invalid_rpc",
                "failed or uncertain delivery requires its exact error evidence",
            )
    elif error_code is not None:
        raise GatewayProtocolError(
            "invalid_rpc",
            "successful or active delivery cannot include an error code",
        )
    return DeliverySnapshot(
        outbound_id=_required_id(raw["outboundId"], "outboundId"),
        session_id=_required_id(raw["sessionId"], "sessionId"),
        run_id=_required_id(raw["runId"], "runId"),
        state=state,
        receipts=receipts,
        provider_message_id=_optional_id(raw, "providerMessageId"),
        error_code=error_code,
    )


def _parse_delivery_receipt_snapshot(value: Any) -> DeliveryReceiptSnapshot:
    raw = _object(value, "delivery receipt")
    _exact_keys(
        raw,
        required={"receiptId", "stage", "observedAtMs"},
        optional={"providerMessageId", "errorCode"},
        label="delivery receipt",
    )
    return DeliveryReceiptSnapshot(
        receipt_id=_required_id(raw["receiptId"], "receiptId"),
        stage=cast(
            DeliveryStage,
            _required_enum(raw["stage"], "stage", _DELIVERY_STAGES),
        ),
        observed_at_ms=_required_int(raw["observedAtMs"], "observedAtMs", 0),
        provider_message_id=_optional_id(raw, "providerMessageId"),
        error_code=_optional_id(raw, "errorCode"),
    )


def _parse_run_snapshot(value: Any) -> RunSnapshot:
    raw = _object(value, "run snapshot")
    _exact_keys(
        raw,
        required={"sessionId", "runId", "state", "segments"},
        optional={"errorCode"},
        label="run snapshot",
    )
    state = cast(ChatRunState, _required_enum(raw["state"], "state", _RUN_STATES))
    segments = _parse_segments(raw["segments"], minimum=0)
    error_code = _optional_id(raw, "errorCode")
    if state == "failed":
        if error_code is None or segments:
            raise GatewayProtocolError(
                "invalid_rpc",
                "failed run snapshot requires only an error code",
            )
    elif error_code is not None:
        raise GatewayProtocolError(
            "invalid_rpc",
            "non-failed run snapshot cannot include an error code",
        )
    if state not in {"completed", "aborted"} and segments:
        raise GatewayProtocolError(
            "invalid_rpc",
            "non-terminal run snapshot cannot include output segments",
        )
    return RunSnapshot(
        session_id=_required_id(raw["sessionId"], "sessionId"),
        run_id=_required_id(raw["runId"], "runId"),
        state=state,
        segments=segments,
        error_code=error_code,
    )


def _parse_approvals_list_result(result: Mapping[str, Any]) -> ApprovalsListResult:
    _exact_keys(
        result,
        required={"approvals"},
        optional={"nextCursor"},
        label="approvals.list result",
    )
    rows = _array(result["approvals"], "approvals", maximum=MAX_RPC_COLLECTION_ITEMS)
    next_cursor = (
        _required_int(result["nextCursor"], "nextCursor", 0) if "nextCursor" in result else None
    )
    return ApprovalsListResult(
        approvals=tuple(_parse_approval_snapshot(item) for item in rows),
        next_cursor=next_cursor,
    )


def _parse_approvals_resolve_result(result: Mapping[str, Any]) -> ApprovalsResolveResult:
    _exact_keys(
        result,
        required={"approvalId", "resolved", "accepted", "code"},
        label="approvals.resolve result",
    )
    return ApprovalsResolveResult(
        approval_id=_required_id(result["approvalId"], "approvalId"),
        resolved=_required_bool(result["resolved"], "resolved"),
        accepted=_required_bool(result["accepted"], "accepted"),
        code=_required_id(result["code"], "code"),
    )


def _serialize_health_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, HealthResult, "health result")
    return {
        "ready": value.ready,
        "serverGeneration": value.server_generation,
        "eventCursor": value.event_cursor,
    }


def _serialize_status_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, StatusResult, "status result")
    return {
        "ready": value.ready,
        "serverGeneration": value.server_generation,
        "eventCursor": value.event_cursor,
        "activeRuns": value.active_runs,
        "sessionCount": value.session_count,
    }


def _serialize_channels_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, ChannelsListResult, "channels.list result")
    return {"channels": [_serialize_channel_snapshot(item) for item in value.channels]}


def _serialize_events_replay_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, EventsReplayResult, "events.replay result")
    return {
        "events": [_serialize_replay_item(item) for item in value.events],
        "nextCursor": value.next_cursor,
        "currentCursor": value.current_cursor,
        "resyncRequired": value.resync_required,
    }


def _serialize_sessions_create_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, SessionsCreateResult, "sessions.create result")
    return {"session": _serialize_session_snapshot(value.session)}


def _serialize_sessions_list_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, SessionsListResult, "sessions.list result")
    payload: dict[str, Any] = {
        "sessions": [_serialize_session_snapshot(item) for item in value.sessions]
    }
    _put_optional(payload, "nextCursor", value.next_cursor)
    return payload


def _serialize_sessions_get_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, SessionsGetResult, "sessions.get result")
    return {"session": _serialize_session_snapshot(value.session)}


def _serialize_sessions_patch_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, SessionsPatchResult, "sessions.patch result")
    return {"session": _serialize_session_snapshot(value.session)}


def _serialize_chat_send_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, ChatSendResult, "chat.send result")
    return {"sessionId": value.session_id, "runId": value.run_id, "status": value.status}


def _serialize_chat_abort_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, ChatAbortResult, "chat.abort result")
    return {
        "sessionId": value.session_id,
        "runId": value.run_id,
        "aborted": value.aborted,
    }


def _serialize_runs_get_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, RunsGetResult, "runs.get result")
    return {"run": _serialize_run_snapshot(value.run)}


def _serialize_runs_latest_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, RunsLatestResult, "runs.latest result")
    return {"run": None if value.run is None else _serialize_run_snapshot(value.run)}


def _serialize_deliveries_get_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, DeliveriesGetResult, "deliveries.get result")
    return {
        "deliveries": [_serialize_delivery_snapshot(item) for item in value.deliveries]
    }


def _serialize_delivery_snapshot(value: DeliverySnapshot) -> dict[str, Any]:
    value = _expect_type(value, DeliverySnapshot, "delivery snapshot")
    payload: dict[str, Any] = {
        "outboundId": value.outbound_id,
        "sessionId": value.session_id,
        "runId": value.run_id,
        "state": value.state,
        "receipts": [
            _serialize_delivery_receipt_snapshot(item) for item in value.receipts
        ],
    }
    _put_optional(payload, "providerMessageId", value.provider_message_id)
    _put_optional(payload, "errorCode", value.error_code)
    return payload


def _serialize_delivery_receipt_snapshot(
    value: DeliveryReceiptSnapshot,
) -> dict[str, Any]:
    value = _expect_type(value, DeliveryReceiptSnapshot, "delivery receipt")
    payload: dict[str, Any] = {
        "receiptId": value.receipt_id,
        "stage": value.stage,
        "observedAtMs": value.observed_at_ms,
    }
    _put_optional(payload, "providerMessageId", value.provider_message_id)
    _put_optional(payload, "errorCode", value.error_code)
    return payload


def _serialize_run_snapshot(run: RunSnapshot) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sessionId": run.session_id,
        "runId": run.run_id,
        "state": run.state,
        "segments": [_serialize_segment(segment) for segment in run.segments],
    }
    _put_optional(payload, "errorCode", run.error_code)
    return payload


def _serialize_approvals_list_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, ApprovalsListResult, "approvals.list result")
    payload: dict[str, Any] = {
        "approvals": [_serialize_approval_snapshot(item) for item in value.approvals]
    }
    _put_optional(payload, "nextCursor", value.next_cursor)
    return payload


def _serialize_approvals_resolve_result(result: GatewayMethodResult) -> dict[str, Any]:
    value = _expect_type(result, ApprovalsResolveResult, "approvals.resolve result")
    return {
        "approvalId": value.approval_id,
        "resolved": value.resolved,
        "accepted": value.accepted,
        "code": value.code,
    }


def _parse_channel_status_event(payload: Mapping[str, Any]) -> ChannelStatusEvent:
    _exact_keys(payload, required={"channel"}, label="channel.status payload")
    return ChannelStatusEvent(_parse_channel_snapshot(payload["channel"]))


def _parse_session_updated_event(payload: Mapping[str, Any]) -> SessionUpdatedEvent:
    _exact_keys(payload, required={"session"}, label="session.updated payload")
    return SessionUpdatedEvent(_parse_session_snapshot(payload["session"]))


def _parse_chat_update_event(payload: Mapping[str, Any]) -> ChatUpdateEvent:
    _exact_keys(
        payload,
        required={"sessionId", "runId", "text"},
        label="chat.update payload",
    )
    return ChatUpdateEvent(
        session_id=_required_id(payload["sessionId"], "sessionId"),
        run_id=_required_id(payload["runId"], "runId"),
        text=_required_text(payload["text"], "text", max_chars=MAX_RPC_TEXT_CHARS),
    )


def _parse_chat_final_event(payload: Mapping[str, Any]) -> ChatFinalEvent:
    _exact_keys(
        payload,
        required={"sessionId", "runId", "stopReason", "segments"},
        label="chat.final payload",
    )
    return ChatFinalEvent(
        session_id=_required_id(payload["sessionId"], "sessionId"),
        run_id=_required_id(payload["runId"], "runId"),
        stop_reason=cast(
            ChatStopReason,
            _required_enum(payload["stopReason"], "stopReason", _STOP_REASONS),
        ),
        segments=_parse_segments(payload["segments"], minimum=0),
    )


def _parse_chat_error_event(payload: Mapping[str, Any]) -> ChatErrorEvent:
    _exact_keys(
        payload,
        required={"sessionId", "runId", "code", "message", "retryable"},
        label="chat.error payload",
    )
    return ChatErrorEvent(
        session_id=_required_id(payload["sessionId"], "sessionId"),
        run_id=_required_id(payload["runId"], "runId"),
        code=_required_id(payload["code"], "code"),
        message=_required_text(payload["message"], "message", max_chars=4096),
        retryable=_required_bool(payload["retryable"], "retryable"),
    )


def _parse_approval_requested_event(payload: Mapping[str, Any]) -> ApprovalRequestedEvent:
    _exact_keys(payload, required={"approval"}, label="approval.requested payload")
    return ApprovalRequestedEvent(_parse_approval_snapshot(payload["approval"]))


def _parse_delivery_updated_event(payload: Mapping[str, Any]) -> DeliveryUpdatedEvent:
    _exact_keys(
        payload,
        required={"outboundId", "receiptId", "stage", "observedAtMs"},
        optional={
            "sessionId",
            "runId",
            "messageId",
            "providerMessageId",
            "errorCode",
        },
        label="delivery.updated payload",
    )
    session_id = _optional_id(payload, "sessionId")
    run_id = _optional_id(payload, "runId")
    if run_id is not None and session_id is None:
        raise GatewayProtocolError(
            "invalid_rpc",
            "delivery runId must be bound to sessionId",
        )
    return DeliveryUpdatedEvent(
        outbound_id=_required_id(payload["outboundId"], "outboundId"),
        receipt_id=_required_id(payload["receiptId"], "receiptId"),
        stage=cast(
            DeliveryStage,
            _required_enum(payload["stage"], "stage", _DELIVERY_STAGES),
        ),
        observed_at_ms=_required_int(payload["observedAtMs"], "observedAtMs", 0),
        session_id=session_id,
        run_id=run_id,
        message_id=_optional_id(payload, "messageId"),
        provider_message_id=_optional_id(payload, "providerMessageId"),
        error_code=_optional_id(payload, "errorCode"),
    )


def _serialize_channel_status_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, ChannelStatusEvent, "channel.status payload")
    return {"channel": _serialize_channel_snapshot(value.channel)}


def _serialize_session_updated_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, SessionUpdatedEvent, "session.updated payload")
    return {"session": _serialize_session_snapshot(value.session)}


def _serialize_chat_update_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, ChatUpdateEvent, "chat.update payload")
    return {"sessionId": value.session_id, "runId": value.run_id, "text": value.text}


def _serialize_chat_final_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, ChatFinalEvent, "chat.final payload")
    return {
        "sessionId": value.session_id,
        "runId": value.run_id,
        "stopReason": value.stop_reason,
        "segments": [_serialize_segment(segment) for segment in value.segments],
    }


def _serialize_chat_error_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, ChatErrorEvent, "chat.error payload")
    return {
        "sessionId": value.session_id,
        "runId": value.run_id,
        "code": value.code,
        "message": value.message,
        "retryable": value.retryable,
    }


def _serialize_approval_requested_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, ApprovalRequestedEvent, "approval.requested payload")
    return {"approval": _serialize_approval_snapshot(value.approval)}


def _serialize_delivery_updated_event(payload: GatewayEventPayload) -> dict[str, Any]:
    value = _expect_type(payload, DeliveryUpdatedEvent, "delivery.updated payload")
    result: dict[str, Any] = {
        "outboundId": value.outbound_id,
        "receiptId": value.receipt_id,
        "stage": value.stage,
        "observedAtMs": value.observed_at_ms,
    }
    _put_optional(result, "sessionId", value.session_id)
    _put_optional(result, "runId", value.run_id)
    _put_optional(result, "messageId", value.message_id)
    _put_optional(result, "providerMessageId", value.provider_message_id)
    _put_optional(result, "errorCode", value.error_code)
    return result


def _parse_replay_item(value: Any) -> GatewayReplayItem:
    payload = _object(value, "events.replay event")
    _exact_keys(
        payload,
        required={"event", "seq", "payload"},
        label="events.replay event",
    )
    event = _required_id(payload["event"], "event")
    if event not in GATEWAY_EVENTS:
        raise GatewayProtocolError("invalid_rpc", "events.replay event is not recognized")
    event_payload = _object(payload["payload"], "events.replay event payload")
    return GatewayReplayItem(
        event=event,
        seq=_required_int(payload["seq"], "seq", 1),
        payload=parse_event_payload(event, event_payload),
    )


def _serialize_replay_item(value: GatewayReplayItem) -> dict[str, Any]:
    value = _expect_type(value, GatewayReplayItem, "events.replay event")
    if value.event not in GATEWAY_EVENTS:
        raise GatewayProtocolError("invalid_rpc", "events.replay event is not recognized")
    return {
        "event": value.event,
        "seq": value.seq,
        "payload": serialize_event_payload(value.event, value.payload),
    }


def _parse_channel_snapshot(value: Any) -> ChannelSnapshot:
    payload = _object(value, "channel snapshot")
    _exact_keys(
        payload,
        required={"account", "state", "capabilities"},
        optional={"errorCode"},
        label="channel snapshot",
    )
    capabilities = _id_tuple(payload["capabilities"], "capabilities", maximum=64)
    return ChannelSnapshot(
        account=_parse_account(payload["account"]),
        state=cast(
            ChannelConnectionState,
            _required_enum(payload["state"], "channel state", _CHANNEL_STATES),
        ),
        capabilities=capabilities,
        error_code=_optional_id(payload, "errorCode"),
    )


def _serialize_channel_snapshot(value: ChannelSnapshot) -> dict[str, Any]:
    value = _expect_type(value, ChannelSnapshot, "channel snapshot")
    result: dict[str, Any] = {
        "account": _serialize_account(value.account),
        "state": value.state,
        "capabilities": list(value.capabilities),
    }
    _put_optional(result, "errorCode", value.error_code)
    return result


def _parse_session_snapshot(value: Any) -> SessionSnapshot:
    payload = _object(value, "session snapshot")
    _exact_keys(
        payload,
        required={
            "sessionId",
            "account",
            "conversation",
            "mode",
            "debug",
            "eventCursor",
        },
        optional={"activeRunId"},
        label="session snapshot",
    )
    return SessionSnapshot(
        session_id=_required_id(payload["sessionId"], "sessionId"),
        account=_parse_account(payload["account"]),
        conversation=_parse_conversation(payload["conversation"]),
        mode=_required_id(payload["mode"], "mode"),
        debug=_required_bool(payload["debug"], "debug"),
        event_cursor=_required_int(payload["eventCursor"], "eventCursor", 0),
        active_run_id=_optional_id(payload, "activeRunId"),
    )


def _serialize_session_snapshot(value: SessionSnapshot) -> dict[str, Any]:
    value = _expect_type(value, SessionSnapshot, "session snapshot")
    result: dict[str, Any] = {
        "sessionId": value.session_id,
        "account": _serialize_account(value.account),
        "conversation": _serialize_conversation(value.conversation),
        "mode": value.mode,
        "debug": value.debug,
        "eventCursor": value.event_cursor,
    }
    _put_optional(result, "activeRunId", value.active_run_id)
    return result


def _parse_approval_snapshot(value: Any) -> ApprovalSnapshot:
    payload = _object(value, "approval snapshot")
    _exact_keys(
        payload,
        required={
            "approvalId",
            "sessionId",
            "operation",
            "target",
            "policyVersion",
            "expiresAtMs",
            "status",
            "allowedDecisions",
        },
        optional={"challenge", "runId"},
        label="approval snapshot",
    )
    decisions = _id_tuple(
        payload["allowedDecisions"],
        "allowedDecisions",
        minimum=0,
        maximum=2,
        allowed=_APPROVAL_DECISIONS,
    )
    status = cast(
        ApprovalStatus,
        _required_enum(payload["status"], "status", _APPROVAL_STATUSES),
    )
    challenge = (
        _required_text(payload["challenge"], "challenge", max_chars=512)
        if "challenge" in payload
        else None
    )
    if status == "pending":
        if decisions != ("approve", "deny") or challenge is None:
            raise GatewayProtocolError(
                "invalid_rpc",
                "pending approval requires approve/deny decisions and a challenge"
            )
    elif decisions or challenge is not None:
        raise GatewayProtocolError(
            "invalid_rpc",
            "terminal approval cannot expose decisions or a challenge"
        )
    return ApprovalSnapshot(
        approval_id=_required_id(payload["approvalId"], "approvalId"),
        session_id=_required_id(payload["sessionId"], "sessionId"),
        operation=_required_id(payload["operation"], "operation"),
        target=_required_text(payload["target"], "target", max_chars=512),
        policy_version=_required_id(payload["policyVersion"], "policyVersion"),
        expires_at_ms=_required_int(payload["expiresAtMs"], "expiresAtMs", 1),
        status=status,
        allowed_decisions=cast(tuple[ApprovalDecisionOption, ...], decisions),
        challenge=challenge,
        run_id=_optional_id(payload, "runId"),
    )


def _serialize_approval_snapshot(value: ApprovalSnapshot) -> dict[str, Any]:
    value = _expect_type(value, ApprovalSnapshot, "approval snapshot")
    result: dict[str, Any] = {
        "approvalId": value.approval_id,
        "sessionId": value.session_id,
        "operation": value.operation,
        "target": value.target,
        "policyVersion": value.policy_version,
        "expiresAtMs": value.expires_at_ms,
        "status": value.status,
        "allowedDecisions": list(value.allowed_decisions),
    }
    _put_optional(result, "challenge", value.challenge)
    _put_optional(result, "runId", value.run_id)
    return result


def _parse_account(value: Any) -> ChannelAccountRef:
    payload = _object(value, "account")
    _exact_keys(payload, required={"channel", "accountId"}, label="account")
    return ChannelAccountRef(
        channel=_required_id(payload["channel"], "channel"),
        account_id=_required_id(payload["accountId"], "accountId"),
    )


def _serialize_account(value: ChannelAccountRef) -> dict[str, Any]:
    value = _expect_type(value, ChannelAccountRef, "account")
    return {"channel": value.channel, "accountId": value.account_id}


def _parse_conversation(value: Any) -> ConversationRef:
    payload = _object(value, "conversation")
    _exact_keys(
        payload,
        required={"kind", "conversationId"},
        label="conversation",
    )
    return ConversationRef(
        kind=_required_id(payload["kind"], "conversation kind"),
        conversation_id=_required_id(payload["conversationId"], "conversationId"),
    )


def _serialize_conversation(value: ConversationRef) -> dict[str, Any]:
    value = _expect_type(value, ConversationRef, "conversation")
    return {"kind": value.kind, "conversationId": value.conversation_id}


def _parse_segments(value: Any, *, minimum: int) -> tuple[CanonicalRpcSegment, ...]:
    rows = _array(value, "segments", minimum=minimum, maximum=MAX_RPC_SEGMENTS)
    return tuple(_parse_segment(item) for item in rows)


def _parse_segment(value: Any) -> CanonicalRpcSegment:
    payload = _object(value, "chat segment")
    kind = _required_enum(
        payload.get("kind"),
        "segment kind",
        {"text", "reply", *_RESOURCE_KINDS},
    )
    if kind == "text":
        _exact_keys(payload, required={"kind", "text"}, label="text segment")
        return TextRpcSegment(
            _required_text(payload["text"], "segment text", max_chars=MAX_RPC_TEXT_CHARS)
        )
    if kind == "reply":
        _exact_keys(payload, required={"kind", "messageId"}, label="reply segment")
        return ReplyRpcSegment(_required_id(payload["messageId"], "messageId"))
    _exact_keys(payload, required={"kind", "resourceId"}, label="resource segment")
    return ResourceRpcSegment(
        kind=cast(ResourceKind, kind),
        resource_id=_required_resource_id(payload["resourceId"]),
    )


def _serialize_segment(value: CanonicalRpcSegment) -> dict[str, Any]:
    if type(value) is TextRpcSegment:
        return {"kind": "text", "text": value.text}
    if type(value) is ReplyRpcSegment:
        return {"kind": "reply", "messageId": value.message_id}
    if type(value) is ResourceRpcSegment:
        return {"kind": value.kind, "resourceId": value.resource_id}
    raise GatewayProtocolError("invalid_rpc", "unsupported canonical chat segment")


def _root_object(value: Any, label: str) -> Mapping[str, Any]:
    _validate_json_limits(value)
    return _object(value, label)


def _validate_json_limits(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    total_items = 0
    observed_bytes = 0
    while stack:
        current, depth = stack.pop()
        total_items += 1
        if total_items > MAX_RPC_TOTAL_ITEMS:
            raise GatewayProtocolError("rpc_too_large", "Gateway RPC has too many values")
        if depth > MAX_RPC_JSON_DEPTH:
            raise GatewayProtocolError("invalid_rpc", "Gateway RPC nesting is too deep")
        if isinstance(current, Mapping):
            if len(current) > MAX_RPC_COLLECTION_ITEMS:
                raise GatewayProtocolError("rpc_too_large", "Gateway RPC object is too large")
            for key, item in current.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise GatewayProtocolError("invalid_rpc", "Gateway RPC key is invalid")
                observed_bytes += _utf8_size(key)
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_RPC_COLLECTION_ITEMS:
                raise GatewayProtocolError("rpc_too_large", "Gateway RPC array is too large")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if len(current) > MAX_RPC_TEXT_CHARS:
                raise GatewayProtocolError("rpc_too_large", "Gateway RPC string is too large")
            observed_bytes += _utf8_size(current)
        elif current is None or type(current) in {bool, int}:
            observed_bytes += len(str(current))
            continue
        else:
            raise GatewayProtocolError("invalid_rpc", "Gateway RPC value is not JSON-safe")
        if observed_bytes > MAX_RPC_BYTES:
            raise GatewayProtocolError("rpc_too_large", "Gateway RPC exceeds the byte limit")
    try:
        serializable = dict(value) if isinstance(value, Mapping) else value
        encoded = json.dumps(
            serializable,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise GatewayProtocolError("invalid_rpc", "Gateway RPC value is not JSON-safe") from exc
    if len(encoded) > MAX_RPC_BYTES:
        raise GatewayProtocolError("rpc_too_large", "Gateway RPC exceeds the byte limit")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise GatewayProtocolError("invalid_rpc", f"{label} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise GatewayProtocolError("invalid_rpc", f"{label} fields are invalid")


def _required_text(value: Any, label: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise GatewayProtocolError("invalid_rpc", f"{label} is invalid")
    return value


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise GatewayProtocolError("invalid_rpc", "Gateway RPC text is not valid UTF-8") from exc


def _required_id(value: Any, label: str) -> str:
    text = _required_text(value, label, max_chars=128)
    if not _ID_RE.fullmatch(text):
        raise GatewayProtocolError("invalid_rpc", f"{label} is invalid")
    return text


def _optional_id(value: Mapping[str, Any], key: str) -> str | None:
    if key not in value:
        return None
    return _required_id(value[key], key)


def _required_resource_id(value: Any) -> str:
    resource_id = _required_id(value, "resourceId")
    if not _OPAQUE_RESOURCE_REF_RE.fullmatch(resource_id):
        raise GatewayProtocolError(
            "invalid_rpc",
            "resourceId must be an opaque Gateway resource reference",
        )
    return resource_id


def _required_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise GatewayProtocolError("invalid_rpc", f"{label} must be a boolean")
    return value


def _optional_bool(
    value: Mapping[str, Any],
    key: str,
    *,
    default: bool | None = None,
) -> bool:
    if key not in value:
        if default is None:
            raise GatewayProtocolError("invalid_rpc", f"{key} is required")
        return default
    return _required_bool(value[key], key)


def _required_int(
    value: Any,
    label: str,
    minimum: int,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int:
        raise GatewayProtocolError("invalid_rpc", f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise GatewayProtocolError("invalid_rpc", f"{label} is out of range")
    return value


def _optional_int(
    value: Mapping[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int = 2**63 - 1,
) -> int:
    if key not in value:
        return default
    return _required_int(value[key], key, minimum, maximum)


def _required_enum(value: Any, label: str, allowed: set[str] | frozenset[str]) -> str:
    text = _required_text(value, label, max_chars=128)
    if text not in allowed:
        raise GatewayProtocolError("invalid_rpc", f"{label} is invalid")
    return text


def _array(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int,
) -> list[Any]:
    if not isinstance(value, list) or len(value) < minimum or len(value) > maximum:
        raise GatewayProtocolError("invalid_rpc", f"{label} must be a bounded array")
    return value


def _id_tuple(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int,
    allowed: set[str] | frozenset[str] | None = None,
) -> tuple[str, ...]:
    rows = _array(value, label, minimum=minimum, maximum=maximum)
    result = tuple(_required_id(item, label) for item in rows)
    if len(set(result)) != len(result):
        raise GatewayProtocolError("invalid_rpc", f"{label} contains duplicates")
    if allowed is not None and not set(result).issubset(allowed):
        raise GatewayProtocolError("invalid_rpc", f"{label} contains an unknown value")
    return result


def _expect_type(value: object, expected: type[_T], label: str) -> _T:
    if type(value) is not expected:
        raise GatewayProtocolError("invalid_rpc", f"{label} has the wrong typed DTO")
    return cast(_T, value)


def _put_optional(payload: dict[str, Any], key: str, value: Any | None) -> None:
    if value is not None:
        payload[key] = value


def _reject_mutation_identity_claims(value: Mapping[str, Any]) -> None:
    stack: list[Mapping[str, Any]] = [value]
    while stack:
        current = stack.pop()
        for key, item in current.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _FORBIDDEN_MUTATION_IDENTITY_KEYS:
                raise GatewayProtocolError(
                    "untrusted_identity",
                    "Gateway client cannot assert actor or transport identity",
                )
            if isinstance(item, Mapping):
                stack.append(item)
            elif isinstance(item, list):
                stack.extend(child for child in item if isinstance(child, Mapping))


_REQUEST_PARSERS: dict[str, _RequestParser] = {
    "health": _parse_health,
    "status": _parse_status,
    "channels.list": _parse_channels_list,
    "events.replay": _parse_events_replay,
    "sessions.create": _parse_sessions_create,
    "sessions.list": _parse_sessions_list,
    "sessions.get": _parse_sessions_get,
    "sessions.patch": _parse_sessions_patch,
    "chat.send": _parse_chat_send,
    "chat.abort": _parse_chat_abort,
    "runs.get": _parse_runs_get,
    "runs.latest": _parse_runs_latest,
    "deliveries.get": _parse_deliveries_get,
    "approvals.list": _parse_approvals_list,
    "approvals.resolve": _parse_approvals_resolve,
}

_REQUEST_SERIALIZERS: dict[str, Callable[[GatewayRequestParams], dict[str, Any]]] = {
    "health": _serialize_health,
    "status": _serialize_status,
    "channels.list": _serialize_channels_list,
    "events.replay": _serialize_events_replay,
    "sessions.create": _serialize_sessions_create,
    "sessions.list": _serialize_sessions_list,
    "sessions.get": _serialize_sessions_get,
    "sessions.patch": _serialize_sessions_patch,
    "chat.send": _serialize_chat_send,
    "chat.abort": _serialize_chat_abort,
    "runs.get": _serialize_runs_get,
    "runs.latest": _serialize_runs_latest,
    "deliveries.get": _serialize_deliveries_get,
    "approvals.list": _serialize_approvals_list,
    "approvals.resolve": _serialize_approvals_resolve,
}

_RESULT_PARSERS: dict[str, _ResultParser] = {
    "health": _parse_health_result,
    "status": _parse_status_result,
    "channels.list": _parse_channels_result,
    "events.replay": _parse_events_replay_result,
    "sessions.create": _parse_sessions_create_result,
    "sessions.list": _parse_sessions_list_result,
    "sessions.get": _parse_sessions_get_result,
    "sessions.patch": _parse_sessions_patch_result,
    "chat.send": _parse_chat_send_result,
    "chat.abort": _parse_chat_abort_result,
    "runs.get": _parse_runs_get_result,
    "runs.latest": _parse_runs_latest_result,
    "deliveries.get": _parse_deliveries_get_result,
    "approvals.list": _parse_approvals_list_result,
    "approvals.resolve": _parse_approvals_resolve_result,
}

_RESULT_SERIALIZERS: dict[str, Callable[[GatewayMethodResult], dict[str, Any]]] = {
    "health": _serialize_health_result,
    "status": _serialize_status_result,
    "channels.list": _serialize_channels_result,
    "events.replay": _serialize_events_replay_result,
    "sessions.create": _serialize_sessions_create_result,
    "sessions.list": _serialize_sessions_list_result,
    "sessions.get": _serialize_sessions_get_result,
    "sessions.patch": _serialize_sessions_patch_result,
    "chat.send": _serialize_chat_send_result,
    "chat.abort": _serialize_chat_abort_result,
    "runs.get": _serialize_runs_get_result,
    "runs.latest": _serialize_runs_latest_result,
    "deliveries.get": _serialize_deliveries_get_result,
    "approvals.list": _serialize_approvals_list_result,
    "approvals.resolve": _serialize_approvals_resolve_result,
}

_EVENT_PARSERS: dict[str, _EventParser] = {
    "channel.status": _parse_channel_status_event,
    "session.updated": _parse_session_updated_event,
    "chat.update": _parse_chat_update_event,
    "chat.final": _parse_chat_final_event,
    "chat.error": _parse_chat_error_event,
    "approval.requested": _parse_approval_requested_event,
    "delivery.updated": _parse_delivery_updated_event,
}

_EVENT_SERIALIZERS: dict[str, Callable[[GatewayEventPayload], dict[str, Any]]] = {
    "channel.status": _serialize_channel_status_event,
    "session.updated": _serialize_session_updated_event,
    "chat.update": _serialize_chat_update_event,
    "chat.final": _serialize_chat_final_event,
    "chat.error": _serialize_chat_error_event,
    "approval.requested": _serialize_approval_requested_event,
    "delivery.updated": _serialize_delivery_updated_event,
}

if (
    tuple(_REQUEST_PARSERS) != GATEWAY_METHODS
    or tuple(_REQUEST_SERIALIZERS) != GATEWAY_METHODS
    or tuple(_RESULT_PARSERS) != GATEWAY_METHODS
    or tuple(_RESULT_SERIALIZERS) != GATEWAY_METHODS
):
    raise RuntimeError("Gateway RPC method registry differs from gateway.protocol")
if tuple(_EVENT_PARSERS) != GATEWAY_EVENTS or tuple(_EVENT_SERIALIZERS) != GATEWAY_EVENTS:
    raise RuntimeError("Gateway RPC event registry differs from gateway.protocol")


__all__ = [
    "MAX_RPC_BYTES",
    "MAX_RPC_COLLECTION_ITEMS",
    "MAX_RPC_JSON_DEPTH",
    "MAX_RPC_SEGMENTS",
    "MAX_RPC_TEXT_CHARS",
    "parse_event_payload",
    "parse_method_result",
    "parse_request_params",
    "serialize_event_payload",
    "serialize_method_result",
    "serialize_request_params",
]
