"""OneBot v11 frame normalization without importing Gateway runtime code."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    OutboundEnvelope,
    ResourceKind,
    ResourceTicket,
    SenderClaim,
    TransportEvidence,
)


_QQ_ID_RE = re.compile(r"^[1-9][0-9]{4,19}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_SEGMENTS = 128
_MAX_TEXT_CHARS = 64 * 1024
_MAX_NATIVE_KIND_CHARS = 64
_MAX_PROVIDER_REF_CHARS = 4096
_MAX_OUTBOUND_SOURCE_CHARS = 8 * 1024 * 1024
_RESOURCE_KIND_BY_SEGMENT: Mapping[str, ResourceKind] = {
    "image": "image",
    "record": "audio",
    "audio": "audio",
    "video": "video",
    "file": "file",
}


class OneBotCodecError(ValueError):
    """Malformed or ambiguous OneBot data rejected before host side effects."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedOneBotFrame:
    payload: Mapping[str, Any]
    frame_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class InboundDecodeResult:
    code: str
    event: CanonicalInboundEvent | None = None


@dataclass(frozen=True)
class OneBotActionResponse:
    echo: str | None
    status: str
    retcode: int | None
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.retcode == 0


def parse_native_frame(
    raw: str | bytes,
    *,
    max_frame_bytes: int,
) -> ParsedOneBotFrame:
    """Decode exactly one bounded UTF-8 JSON object and bind its raw digest."""

    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise OneBotCodecError("onebot_frame_type_invalid", "OneBot frame must be text or bytes")
    if len(encoded) > max_frame_bytes:
        raise OneBotCodecError("onebot_frame_too_large", "OneBot frame exceeds the configured limit")
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OneBotCodecError("onebot_frame_not_utf8", "OneBot frame must be UTF-8") from exc
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise OneBotCodecError("onebot_frame_json_invalid", "OneBot frame must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise OneBotCodecError("onebot_frame_not_object", "OneBot frame must be a JSON object")
    return ParsedOneBotFrame(
        payload=payload,
        frame_sha256=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
    )


def decode_action_response(frame: ParsedOneBotFrame) -> OneBotActionResponse | None:
    """Return an action response without confusing unsolicited events for replies."""

    payload = frame.payload
    if "status" not in payload and "retcode" not in payload:
        return None
    raw_echo = payload.get("echo")
    echo = None if raw_echo is None else _bounded_scalar(raw_echo, field="echo", max_chars=256)
    status = str(payload.get("status") or "").strip().lower()
    raw_retcode = payload.get("retcode")
    if raw_retcode is None:
        retcode = None
    else:
        try:
            retcode = int(str(raw_retcode))
        except (TypeError, ValueError):
            retcode = -1
    return OneBotActionResponse(
        echo=echo,
        status=status,
        retcode=retcode,
        data=payload.get("data"),
    )


def decode_inbound_message(
    frame: ParsedOneBotFrame,
    *,
    account_id: str,
    connection_generation: str,
    observed_at: float,
    resource_ticket_ttl_seconds: float,
) -> InboundDecodeResult:
    """Normalize an eligible OneBot message while keeping sender as a claim."""

    payload = frame.payload
    if payload.get("post_type") != "message":
        return InboundDecodeResult("not_message")

    verified_account_id = _qq_id(account_id, field="account_id")
    if payload.get("self_id") is not None:
        native_account_id = _qq_id(payload.get("self_id"), field="self_id")
        if native_account_id != verified_account_id:
            raise OneBotCodecError(
                "onebot_event_account_mismatch",
                "OneBot event account does not match the verified connection account",
            )

    message_type = str(payload.get("message_type") or "").strip().lower()
    if message_type not in {"private", "group"}:
        return InboundDecodeResult("unsupported_message_type")

    native_message = payload.get("message")
    if message_type == "group" and not _has_structured_self_mention(
        native_message,
        verified_account_id,
    ):
        return InboundDecodeResult("group_mention_missing")

    sender_id = _qq_id(payload.get("user_id"), field="user_id")
    sender_data = payload.get("sender")
    if sender_data is not None and not isinstance(sender_data, dict):
        raise OneBotCodecError("onebot_sender_invalid", "sender must be an object when present")
    if isinstance(sender_data, dict) and sender_data.get("user_id") is not None:
        nested_sender_id = _qq_id(sender_data.get("user_id"), field="sender.user_id")
        if nested_sender_id != sender_id:
            raise OneBotCodecError(
                "onebot_sender_mismatch",
                "top-level and nested OneBot sender identifiers do not match",
            )

    if message_type == "group":
        conversation_id = _qq_id(payload.get("group_id"), field="group_id")
        conversation_kind = "group"
    else:
        conversation_id = sender_id
        conversation_kind = "p2p"

    message_id = _message_id(payload.get("message_id"))
    account = ChannelAccountRef(channel="qq", account_id=verified_account_id)
    conversation = ConversationRef(
        kind=conversation_kind,
        conversation_id=conversation_id,
    )
    sender = SenderClaim(
        sender_id=sender_id,
        display_name=_display_name(sender_data),
    )
    event_id = _event_id(account, conversation, message_id)
    evidence = TransportEvidence(
        account=account,
        conversation=conversation,
        sender=sender,
        event_id=event_id,
        message_id=message_id,
        connection_generation=_bounded_scalar(
            connection_generation,
            field="connection_generation",
            max_chars=128,
        ),
        frame_sha256=frame.frame_sha256,
        observed_at=float(observed_at),
    )
    segments, tickets = _canonical_segments(
        native_message,
        evidence=evidence,
        expires_at=float(observed_at) + float(resource_ticket_ttl_seconds),
    )
    if not segments:
        return InboundDecodeResult("empty_message")
    return InboundDecodeResult(
        "accepted",
        CanonicalInboundEvent(
            evidence=evidence,
            segments=segments,
            resource_tickets=tickets,
        ),
    )


def encode_action_request(action: str, params: Mapping[str, Any], *, echo: str) -> str:
    if _ACTION_RE.fullmatch(action) is None:
        raise OneBotCodecError("onebot_action_invalid", "OneBot action name is invalid")
    normalized_echo = _bounded_scalar(echo, field="echo", max_chars=256)
    return json.dumps(
        {"action": action, "params": dict(params), "echo": normalized_echo},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_outbound_action(envelope: OutboundEnvelope) -> tuple[str, Mapping[str, Any]]:
    """Project an authorized provider-neutral envelope to OneBot ``send_msg``."""

    conversation_id = _qq_id(
        envelope.conversation.conversation_id,
        field="conversation_id",
    )
    if envelope.conversation.kind == "p2p":
        params: dict[str, Any] = {
            "message_type": "private",
            "user_id": conversation_id,
        }
    elif envelope.conversation.kind == "group":
        params = {
            "message_type": "group",
            "group_id": conversation_id,
        }
    else:
        raise OneBotCodecError(
            "onebot_outbound_conversation_invalid",
            "QQ outbound conversation must be p2p or group",
        )

    if len(envelope.segments) > _MAX_SEGMENTS:
        raise OneBotCodecError(
            "onebot_outbound_segments_too_many",
            "Outbound message has too many segments",
        )
    native_segments: list[dict[str, Any]] = []
    total_resource_chars = 0
    if envelope.reply_to_message_id is not None:
        native_segments.append(
            {
                "type": "reply",
                "data": {"id": _message_id(envelope.reply_to_message_id)},
            }
        )
    for segment in envelope.segments:
        if segment.kind == "text":
            text = segment.text
            if not isinstance(text, str) or not text or len(text) > _MAX_TEXT_CHARS:
                raise OneBotCodecError(
                    "onebot_outbound_text_invalid",
                    "Outbound text segment is empty or too large",
                )
            native_segments.append({"type": "text", "data": {"text": text}})
        elif segment.kind == "mention":
            target = _qq_id(segment.target, field="mention.target")
            native_segments.append({"type": "at", "data": {"qq": target}})
        elif segment.kind == "reply":
            if envelope.reply_to_message_id is not None or any(
                item.get("type") == "reply" for item in native_segments
            ):
                raise OneBotCodecError(
                    "onebot_outbound_reply_duplicate",
                    "Outbound message may contain only one reply target",
                )
            native_segments.append(
                {
                    "type": "reply",
                    "data": {"id": _message_id(segment.target)},
                }
            )
        elif segment.kind in {"image", "audio", "video", "file"}:
            source = segment.data.get("source")
            if not isinstance(source, str) or not source or len(source) > _MAX_OUTBOUND_SOURCE_CHARS:
                raise OneBotCodecError(
                    "onebot_outbound_resource_invalid",
                    "Outbound resource segment requires a bounded authorized source",
                )
            if not source.startswith("base64://"):
                raise OneBotCodecError(
                    "onebot_outbound_resource_source_invalid",
                    "Outbound OneBot resources must be materialized as base64 data",
                )
            encoded_resource = source.removeprefix("base64://")
            if not encoded_resource:
                raise OneBotCodecError(
                    "onebot_outbound_resource_base64_invalid",
                    "Outbound OneBot resource base64 data is invalid",
                )
            try:
                base64.b64decode(encoded_resource, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise OneBotCodecError(
                    "onebot_outbound_resource_base64_invalid",
                    "Outbound OneBot resource base64 data is invalid",
                ) from exc
            total_resource_chars += len(source)
            if total_resource_chars > _MAX_OUTBOUND_SOURCE_CHARS:
                raise OneBotCodecError(
                    "onebot_outbound_resources_too_large",
                    "Outbound OneBot resources exceed the combined size limit",
                )
            native_kind = "record" if segment.kind == "audio" else segment.kind
            resource_data: dict[str, Any] = {"file": source}
            name = segment.data.get("name")
            if isinstance(name, str) and name.strip():
                safe_name = _safe_name(name)
                if safe_name is not None:
                    resource_data["name"] = safe_name
            native_segments.append({"type": native_kind, "data": resource_data})
        else:
            raise OneBotCodecError(
                "onebot_outbound_segment_unsupported",
                "Outbound segment is not supported by the OneBot Channel",
            )
    if not native_segments:
        raise OneBotCodecError("onebot_outbound_empty", "Outbound message has no segments")
    params["message"] = native_segments
    return "send_msg", params


def _canonical_segments(
    native_message: Any,
    *,
    evidence: TransportEvidence,
    expires_at: float,
) -> tuple[tuple[MessageSegment, ...], tuple[ResourceTicket, ...]]:
    if isinstance(native_message, str):
        if not native_message:
            return (), ()
        if len(native_message) > _MAX_TEXT_CHARS:
            raise OneBotCodecError("onebot_text_too_large", "OneBot text segment is too large")
        return (MessageSegment(kind="text", text=native_message),), ()
    if not isinstance(native_message, list):
        raise OneBotCodecError("onebot_message_invalid", "OneBot message must be text or segments")
    if len(native_message) > _MAX_SEGMENTS:
        raise OneBotCodecError("onebot_segments_too_many", "OneBot message has too many segments")

    segments: list[MessageSegment] = []
    tickets: list[ResourceTicket] = []
    for index, native_segment in enumerate(native_message):
        if not isinstance(native_segment, dict):
            raise OneBotCodecError("onebot_segment_invalid", "OneBot segment must be an object")
        raw_native_kind = native_segment.get("type")
        if not isinstance(raw_native_kind, str):
            raise OneBotCodecError(
                "onebot_segment_invalid",
                "OneBot segment type must be text",
            )
        native_kind = raw_native_kind.strip().lower()
        native_data = native_segment.get("data")
        if not native_kind or not _is_printable(native_kind) or not isinstance(native_data, dict):
            raise OneBotCodecError(
                "onebot_segment_invalid",
                "OneBot segment requires type and object data",
            )
        if native_kind == "text":
            text = native_data.get("text")
            if not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS:
                raise OneBotCodecError("onebot_text_invalid", "OneBot text segment is invalid")
            if text:
                segments.append(MessageSegment(kind="text", text=text))
            continue
        if native_kind == "at":
            target = _mention_target(native_data.get("qq"))
            segments.append(
                MessageSegment(
                    kind="mention",
                    target=target,
                    data={"scope": "all" if target == "all" else "user"},
                )
            )
            continue
        if native_kind == "reply":
            segments.append(
                MessageSegment(
                    kind="reply",
                    target=_message_id(native_data.get("id")),
                )
            )
            continue
        resource_kind = _RESOURCE_KIND_BY_SEGMENT.get(native_kind)
        if resource_kind is not None:
            ticket = _resource_ticket(
                native_data,
                kind=resource_kind,
                native_kind=native_kind,
                index=index,
                evidence=evidence,
                expires_at=expires_at,
            )
            tickets.append(ticket)
            segments.append(
                MessageSegment(
                    kind=resource_kind,
                    resource_ticket_id=ticket.ticket_id,
                )
            )
            continue
        segments.append(
            MessageSegment(
                kind="unknown",
                data={"native_kind": native_kind[:_MAX_NATIVE_KIND_CHARS]},
            )
        )
    return tuple(segments), tuple(tickets)


def _resource_ticket(
    native_data: Mapping[str, Any],
    *,
    kind: ResourceKind,
    native_kind: str,
    index: int,
    evidence: TransportEvidence,
    expires_at: float,
) -> ResourceTicket:
    ticket_digest = hashlib.sha256(
        (
            f"{evidence.event_id}\0{evidence.frame_sha256}\0{index}\0{native_kind}"
        ).encode("utf-8")
    ).hexdigest()
    provider_ref: dict[str, Any] = {}
    for key in ("file", "url", "file_id", "path", "uuid"):
        value = native_data.get(key)
        if value is None:
            continue
        provider_ref[key] = _bounded_scalar(
            value,
            field=f"resource.{key}",
            max_chars=_MAX_PROVIDER_REF_CHARS,
        )

    name_value = native_data.get("name") or native_data.get("filename")
    if name_value is None:
        name_value = provider_ref.get("file") or provider_ref.get("url")
    name = _safe_name(str(name_value)) if name_value else None
    media_type_value = native_data.get("mime_type") or native_data.get("mime")
    media_type = None
    if isinstance(media_type_value, str):
        candidate = media_type_value.strip().lower()
        if 0 < len(candidate) <= 127 and "/" in candidate and _is_printable(candidate):
            media_type = candidate

    size_bytes = None
    raw_size = native_data.get("file_size", native_data.get("size"))
    if raw_size is not None and not isinstance(raw_size, bool):
        try:
            candidate_size = int(str(raw_size))
        except (TypeError, ValueError):
            candidate_size = -1
        if candidate_size >= 0:
            size_bytes = candidate_size

    sha256_value = native_data.get("sha256")
    sha256 = (
        str(sha256_value).lower()
        if sha256_value is not None and _SHA256_RE.fullmatch(str(sha256_value))
        else None
    )
    return ResourceTicket(
        ticket_id=f"resource_{ticket_digest[:32]}",
        account=evidence.account,
        conversation=evidence.conversation,
        sender_id=evidence.sender.sender_id,
        event_id=evidence.event_id,
        message_id=evidence.message_id,
        kind=kind,
        name=name,
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
        expires_at=expires_at,
        provider_ref=provider_ref,
    )


def _has_structured_self_mention(native_message: Any, account_id: str) -> bool:
    if not isinstance(native_message, list):
        return False
    for segment in native_message:
        if not isinstance(segment, dict) or segment.get("type") != "at":
            continue
        data = segment.get("data")
        if isinstance(data, dict) and str(data.get("qq", "")).strip() == account_id:
            return True
    return False


def _event_id(
    account: ChannelAccountRef,
    conversation: ConversationRef,
    message_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            f"{account.channel}\0{account.account_id}\0{conversation.kind}\0"
            f"{conversation.conversation_id}\0{message_id}"
        ).encode("utf-8")
    ).hexdigest()
    return f"event_{digest[:32]}"


def _display_name(sender_data: Mapping[str, Any] | None) -> str | None:
    if not sender_data:
        return None
    for key in ("card", "nickname"):
        value = sender_data.get(key)
        if not isinstance(value, str):
            continue
        candidate = " ".join(value.split())
        if candidate and _is_printable(candidate):
            return candidate[:128]
    return None


def _mention_target(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized == "all":
        return normalized
    return _qq_id(value, field="mention.qq")


def _qq_id(value: Any, *, field: str) -> str:
    if isinstance(value, bool):
        normalized = ""
    else:
        normalized = str(value or "").strip()
    if _QQ_ID_RE.fullmatch(normalized) is None:
        raise OneBotCodecError(
            "onebot_identity_invalid",
            f"{field} must be a stable numeric QQ identifier",
        )
    return normalized


def _message_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise OneBotCodecError("onebot_message_id_missing", "OneBot message_id is required")
    return _bounded_scalar(value, field="message_id", max_chars=256)


def _bounded_scalar(value: Any, *, field: str, max_chars: int) -> str:
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise OneBotCodecError("onebot_scalar_invalid", f"{field} must be a scalar")
    normalized = str(value).strip()
    if not normalized or len(normalized) > max_chars or not _is_printable(normalized):
        raise OneBotCodecError("onebot_scalar_invalid", f"{field} is missing or invalid")
    return normalized


def _safe_name(value: str) -> str | None:
    candidate = value.strip().replace("\\", "/")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.scheme:
        candidate = parsed.path
    name = PurePosixPath(candidate).name.strip()
    if not name or name in {".", ".."} or not _is_printable(name):
        return None
    return name[:255]


def _is_printable(value: str) -> bool:
    return all(character.isprintable() for character in value)


__all__ = [
    "InboundDecodeResult",
    "OneBotActionResponse",
    "OneBotCodecError",
    "ParsedOneBotFrame",
    "build_outbound_action",
    "decode_action_response",
    "decode_inbound_message",
    "encode_action_request",
    "parse_native_frame",
]
