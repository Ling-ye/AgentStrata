"""Text-only official QQ events; identity claims remain subject to Gateway admission."""
import hashlib
import json
import re
from urllib.parse import quote

from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent, ChannelAccountRef, ConversationRef, MessageSegment,
    SenderClaim, TransportEvidence, OutboundEnvelope,
)
from .config import QQOfficialError

_ID = re.compile(r"[A-Za-z0-9_.:=-]{1,512}")


def _identifier(value, *, message=False):
    if not isinstance(value, str) or not (_ID if message else re.compile(r"[A-Za-z0-9_-]{1,256}")).fullmatch(value):
        raise QQOfficialError("qq_official_event_invalid")
    return value


def decode_event(frame: dict, app_id: str, generation: str, observed_at: float):
    kind = frame.get("t")
    if kind not in {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
        return None
    data = frame.get("d")
    if not isinstance(data, dict) or not isinstance(data.get("author"), dict):
        raise QQOfficialError("qq_official_event_invalid")
    group = kind == "GROUP_AT_MESSAGE_CREATE"
    sender = _identifier(data["author"].get("member_openid" if group else "user_openid"))
    peer = _identifier(data.get("group_openid")) if group else sender
    message = _identifier(data.get("id"), message=True)
    content = data.get("content")
    if not isinstance(content, str) or len(content) > 65536:
        raise QQOfficialError("qq_official_content_invalid")
    if not content.strip():
        return None
    canonical = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=ChannelAccountRef("qq_official", app_id),
            conversation=ConversationRef("group" if group else "p2p", peer),
            sender=SenderClaim(sender), event_id=message, message_id=message,
            connection_generation=generation,
            frame_sha256=hashlib.sha256(canonical.encode()).hexdigest(), observed_at=observed_at,
        ), segments=(MessageSegment("text", text=content.strip()),),
    )


def outbound_request(envelope: OutboundEnvelope, app_id: str):
    if envelope.account != ChannelAccountRef("qq_official", app_id):
        raise QQOfficialError("qq_official_outbound_binding_invalid")
    if envelope.conversation.kind not in {"group", "p2p"}:
        raise QQOfficialError("qq_official_outbound_binding_invalid")
    peer = _identifier(envelope.conversation.conversation_id)
    message = _identifier(envelope.reply_to_message_id, message=True)
    if not envelope.segments or any(s.kind != "text" for s in envelope.segments):
        raise QQOfficialError("qq_official_text_only")
    text = "".join(s.text or "" for s in envelope.segments)
    if not text.strip() or len(text.encode()) > 4000:
        raise QQOfficialError("qq_official_text_limit")
    resource = "groups" if envelope.conversation.kind == "group" else "users"
    return f"/v2/{resource}/{quote(peer, safe='')}/messages", {
        "content": text, "msg_type": 0, "msg_id": message, "msg_seq": 1,
    }



