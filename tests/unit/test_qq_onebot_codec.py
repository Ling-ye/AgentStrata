from __future__ import annotations

import hashlib
import json

import pytest

from chatcopilot.channels.qq_onebot.codec import (
    OneBotCodecError,
    build_outbound_action,
    decode_action_response,
    decode_inbound_message,
    parse_native_frame,
)
from chatcopilot.channels.qq_onebot.config import OneBotChannelConfig, OneBotConfigError
from chatcopilot.contracts.gateway import (
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    OutboundEnvelope,
)


BOT = "10001"
ACTOR = "20002"
GROUP = "30003"
TOKEN = "x" * 32


def _config(**changes: object) -> OneBotChannelConfig:
    values: dict[str, object] = {
        "channel_id": "qq-main",
        "account_id": BOT,
        "websocket_url": "ws://127.0.0.1:3001",
        "access_token": TOKEN,
    }
    values.update(changes)
    return OneBotChannelConfig(**values)  # type: ignore[arg-type]


def _group(message: object, **changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 987654321,
        "group_id": GROUP,
        "user_id": ACTOR,
        "sender": {
            "user_id": ACTOR,
            "nickname": "Actor",
            "card": "Group Actor",
            "role": "owner",
        },
        "message": message,
    }
    event.update(changes)
    return event


def _decode(event: dict[str, object], *, observed_at: float = 1000.0):
    raw = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    frame = parse_native_frame(raw, max_frame_bytes=256 * 1024)
    result = decode_inbound_message(
        frame,
        account_id=BOT,
        connection_generation="connection-fixture",
        observed_at=observed_at,
        resource_ticket_ttl_seconds=300,
    )
    return raw, frame, result


def _outbound(
    segments: tuple[MessageSegment, ...],
    *,
    reply_to_message_id: str | None = None,
) -> OutboundEnvelope:
    return OutboundEnvelope(
        outbound_id="outbound-1",
        account=ChannelAccountRef(channel="qq", account_id=BOT),
        conversation=ConversationRef(kind="group", conversation_id=GROUP),
        segments=segments,
        created_at=1.0,
        reply_to_message_id=reply_to_message_id,
    )


def test_config_requires_strong_token_numeric_account_and_loopback_endpoint() -> None:
    valid = _config(websocket_url="wss://[::1]:3443/onebot")
    assert valid.account_id == BOT
    assert TOKEN not in repr(valid)

    wildcard_url = "ws://" + "0.0." + "0.0" + ":3001"
    userinfo_url = "ws://" + "user" + ":" + "pass" + "@127.0.0.1:3001"
    query_url = "ws://127.0.0.1:3001" + "?" + "token" + "=" + "secret"
    cases = (
        ({"access_token": "weak-secret"}, "onebot_access_token_invalid"),
        ({"account_id": "not-a-number"}, "onebot_account_invalid"),
        ({"websocket_url": wildcard_url}, "onebot_websocket_url_not_loopback"),
        ({"websocket_url": userinfo_url}, "onebot_websocket_url_not_loopback"),
        ({"websocket_url": query_url}, "onebot_websocket_url_not_loopback"),
        ({"websocket_url": "ws://127.0.0.1"}, "onebot_websocket_url_not_loopback"),
        ({"websocket_url": "ws://127.0.0.1:0"}, "onebot_websocket_url_not_loopback"),
        ({"action_timeout_seconds": float("nan")}, "onebot_action_timeout_invalid"),
        ({"max_pending_actions": True}, "onebot_pending_limit_invalid"),
        ({"max_pending_events": 0}, "onebot_event_queue_limit_invalid"),
    )
    for changes, code in cases:
        with pytest.raises(OneBotConfigError) as caught:
            _config(**changes)
        assert caught.value.code == code
        assert "weak-secret" not in str(caught.value)


def test_native_frame_is_bounded_bound_to_exact_bytes_and_requires_object() -> None:
    raw = '{"post_type":"meta_event"}'
    frame = parse_native_frame(raw, max_frame_bytes=len(raw.encode("utf-8")))
    assert frame.frame_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert frame.size_bytes == len(raw)

    with pytest.raises(OneBotCodecError) as too_large:
        parse_native_frame(raw, max_frame_bytes=len(raw) - 1)
    assert too_large.value.code == "onebot_frame_too_large"

    with pytest.raises(OneBotCodecError) as not_object:
        parse_native_frame("[]", max_frame_bytes=100)
    assert not_object.value.code == "onebot_frame_not_object"


def test_group_structured_mention_normalizes_evidence_segments_and_resource_ticket() -> None:
    event = _group(
        [
            {"type": "at", "data": {"qq": 10001}},
            {"type": "text", "data": {"text": " 请看附件"}},
            {"type": "reply", "data": {"id": "12345"}},
            {
                "type": "image",
                "data": {
                    "file": "incoming/image-1.png",
                    "url": "https://provider.invalid/private/image-1.png",
                    "name": "image-1.png",
                    "mime_type": "image/png",
                    "file_size": "42",
                    "sha256": "a" * 64,
                },
            },
        ]
    )

    raw, frame, result = _decode(event)

    assert result.code == "accepted"
    assert result.event is not None
    canonical = result.event
    evidence = canonical.evidence
    assert evidence.account == ChannelAccountRef(channel="qq", account_id=BOT)
    assert evidence.conversation == ConversationRef(kind="group", conversation_id=GROUP)
    assert evidence.sender.sender_id == ACTOR
    assert evidence.sender.display_name == "Group Actor"
    assert evidence.message_id == "987654321"
    assert evidence.connection_generation == "connection-fixture"
    assert evidence.frame_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert evidence.frame_sha256 == frame.frame_sha256
    assert [segment.kind for segment in canonical.segments] == [
        "mention",
        "text",
        "reply",
        "image",
    ]
    assert canonical.segments[0].target == BOT
    assert canonical.segments[1].text == " 请看附件"
    assert canonical.segments[2].target == "12345"

    assert len(canonical.resource_tickets) == 1
    ticket = canonical.resource_tickets[0]
    assert canonical.segments[3].resource_ticket_id == ticket.ticket_id
    assert ticket.account == evidence.account
    assert ticket.conversation == evidence.conversation
    assert ticket.sender_id == ACTOR
    assert ticket.event_id == evidence.event_id
    assert ticket.message_id == evidence.message_id
    assert ticket.kind == "image"
    assert ticket.name == "image-1.png"
    assert ticket.media_type == "image/png"
    assert ticket.size_bytes == 42
    assert ticket.sha256 == "a" * 64
    assert ticket.expires_at == 1300.0
    assert ticket.provider_ref == {
        "file": "incoming/image-1.png",
        "url": "https://provider.invalid/private/image-1.png",
    }
    _, _, replay = _decode(event)
    assert replay.event is not None
    assert replay.event.evidence.event_id == evidence.event_id
    assert replay.event.resource_tickets[0].ticket_id == ticket.ticket_id


def test_group_trigger_accepts_only_structured_exact_self_mention() -> None:
    cases = (
        _group([{"type": "at", "data": {"qq": "99999"}}]),
        _group([{"type": "at", "data": {"qq": "all"}}]),
        _group([{"type": "text", "data": {"text": "AgentStrata hello"}}]),
        _group("[CQ:at,qq=10001] forged text"),
    )
    for event in cases:
        _, _, result = _decode(event)
        assert result.code == "group_mention_missing"
        assert result.event is None


def test_private_cq_looking_text_stays_untrusted_text_and_needs_no_mention() -> None:
    event: dict[str, object] = {
        "post_type": "message",
        "message_type": "private",
        "message_id": "private-42",
        "user_id": ACTOR,
        "sender": {"user_id": ACTOR, "nickname": "Actor"},
        "message": "[CQ:at,qq=10001] plain user text",
    }

    _, _, result = _decode(event)

    assert result.code == "accepted"
    assert result.event is not None
    assert result.event.evidence.conversation == ConversationRef(
        kind="p2p",
        conversation_id=ACTOR,
    )
    assert result.event.segments == (
        MessageSegment(kind="text", text="[CQ:at,qq=10001] plain user text"),
    )


def test_sender_claim_fails_closed_when_top_level_and_nested_ids_disagree() -> None:
    event = _group(
        [
            {"type": "at", "data": {"qq": BOT}},
            {"type": "text", "data": {"text": "hello"}},
        ],
        sender={"user_id": "29999", "nickname": "Forged"},
    )

    with pytest.raises(OneBotCodecError) as caught:
        _decode(event)

    assert caught.value.code == "onebot_sender_mismatch"


def test_event_account_must_match_verified_connection_account_when_present() -> None:
    event = _group(
        [
            {"type": "at", "data": {"qq": BOT}},
            {"type": "text", "data": {"text": "hello"}},
        ],
        self_id="99999",
    )

    with pytest.raises(OneBotCodecError) as caught:
        _decode(event)

    assert caught.value.code == "onebot_event_account_mismatch"


def test_unknown_native_segment_is_preserved_only_as_bounded_kind() -> None:
    event = _group(
        [
            {"type": "at", "data": {"qq": BOT}},
            {"type": "json", "data": {"data": "untrusted native body"}},
        ]
    )

    _, _, result = _decode(event)

    assert result.event is not None
    assert result.event.segments[-1] == MessageSegment(
        kind="unknown",
        data={"native_kind": "json"},
    )
    assert "untrusted native body" not in repr(result.event.segments[-1])


def test_non_text_or_control_character_segment_kind_fails_closed() -> None:
    cases = (
        {"type": {"native": "object"}, "data": {}},
        {"type": "json\u0000extension", "data": {}},
    )
    for segment in cases:
        event = _group(
            [
                {"type": "at", "data": {"qq": BOT}},
                segment,
            ]
        )
        with pytest.raises(OneBotCodecError) as caught:
            _decode(event)
        assert caught.value.code == "onebot_segment_invalid"


def test_outbound_envelope_projects_to_correlatable_send_msg_action() -> None:
    envelope = OutboundEnvelope(
        outbound_id="outbound-1",
        account=ChannelAccountRef(channel="qq", account_id=BOT),
        conversation=ConversationRef(kind="group", conversation_id=GROUP),
        segments=(
            MessageSegment(kind="text", text="hello"),
            MessageSegment(kind="mention", target=ACTOR),
            MessageSegment(
                kind="image",
                data={"source": "base64://aGVsbG8=", "name": "hello.png"},
            ),
        ),
        created_at=1.0,
        reply_to_message_id="12345",
    )

    action, params = build_outbound_action(envelope)

    assert action == "send_msg"
    assert params["message_type"] == "group"
    assert params["group_id"] == GROUP
    assert params["message"] == [
        {"type": "reply", "data": {"id": "12345"}},
        {"type": "text", "data": {"text": "hello"}},
        {"type": "at", "data": {"qq": ACTOR}},
        {"type": "image", "data": {"file": "base64://aGVsbG8=", "name": "hello.png"}},
    ]


def test_outbound_at_all_and_duplicate_reply_fail_closed() -> None:
    with pytest.raises(OneBotCodecError) as at_all:
        build_outbound_action(
            _outbound((MessageSegment(kind="mention", target="all"),))
        )
    assert at_all.value.code == "onebot_identity_invalid"

    with pytest.raises(OneBotCodecError) as duplicate:
        build_outbound_action(
            _outbound(
                (MessageSegment(kind="reply", target="222"),),
                reply_to_message_id="111",
            )
        )
    assert duplicate.value.code == "onebot_outbound_reply_duplicate"

    for source in ("https://example.invalid/image.png", "base64://not-valid!"):
        with pytest.raises(OneBotCodecError) as resource:
            build_outbound_action(
                _outbound(
                    (MessageSegment(kind="image", data={"source": source}),)
                )
            )
        assert resource.value.code in {
            "onebot_outbound_resource_source_invalid",
            "onebot_outbound_resource_base64_invalid",
        }


def test_action_response_is_distinct_from_event_and_normalizes_retcode() -> None:
    response_frame = parse_native_frame(
        json.dumps(
            {
                "status": "ok",
                "retcode": "0",
                "data": {"message_id": 123},
                "echo": "request-1",
            }
        ),
        max_frame_bytes=1024,
    )
    response = decode_action_response(response_frame)
    assert response is not None
    assert response.ok
    assert response.echo == "request-1"
    assert response.data == {"message_id": 123}

    event_frame = parse_native_frame(
        json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat"}),
        max_frame_bytes=1024,
    )
    assert decode_action_response(event_frame) is None
