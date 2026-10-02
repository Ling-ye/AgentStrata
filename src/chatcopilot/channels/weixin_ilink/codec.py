"""Pure conversion from iLink messages to existing Channel contracts."""

from __future__ import annotations

import hashlib
import json

from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    ResourceTicket,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.weixin import WeixinChannelConfig, WeixinError, identity


def decode_message(
    message: dict, config: WeixinChannelConfig, generation: str, now: float
) -> CanonicalInboundEvent | None:
    if not isinstance(message, dict) or type(message.get("message_type")) is not int or message["message_type"] != 1:
        return None
    sender = identity(message.get("from_user_id"))
    if message.get("to_user_id") not in (None, "", config.account_id):
        raise WeixinError("weixin_account_mismatch")
    message_id = message.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        raise WeixinError("weixin_message_id_missing")
    message_id = identity(str(message_id))
    group = message.get("group_id")
    conversation = (
        ConversationRef("group", identity(group)) if group not in (None, "") else ConversationRef("p2p", sender)
    )
    account = ChannelAccountRef("weixin", config.account_id)
    items = message.get("item_list")
    if not isinstance(items, list) or len(items) > 512:
        raise WeixinError("weixin_message_items_invalid")
    segments, tickets = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise WeixinError("weixin_message_items_invalid")
        kind = item.get("type")
        if type(kind) is not int:
            raise WeixinError("weixin_message_items_invalid")
        if kind == 1:
            text_item = item.get("text_item")
            if not isinstance(text_item, dict):
                raise WeixinError("weixin_message_text_invalid")
            text = text_item.get("text")
            if not isinstance(text, str) or len(text) > 256 * 1024:
                raise WeixinError("weixin_message_text_invalid")
            segments.append(MessageSegment("text", text=text))
        elif kind in (2, 4):
            media_kind = "image" if kind == 2 else "file"
            details = item.get("image_item" if kind == 2 else "file_item")
            if not isinstance(details, dict) or not isinstance(details.get("media"), dict):
                raise WeixinError("weixin_media_reference_invalid")
            name = details.get("file_name") if kind == 4 else f"image-{message_id}-{index}.jpg"
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 255
                or any(c in name for c in "/\\\x00")
                or name in (".", "..")
            ):
                raise WeixinError("weixin_filename_invalid")
            ticket_id = (
                "weixin_"
                + hashlib.sha256(f"{config.account_id}:{message_id}:{index}".encode()).hexdigest()[
                    :32
                ]
            )
            tickets.append(
                ResourceTicket(
                    ticket_id,
                    account,
                    conversation,
                    sender,
                    message_id,
                    message_id,
                    media_kind,
                    name=name if kind == 4 else None,
                    provider_ref={"media": dict(details["media"]), "aeskey": details.get("aeskey")},
                )
            )
            segments.append(MessageSegment(media_kind, resource_ticket_id=ticket_id))
    if not segments:
        return None
    fingerprint = {
        key: message.get(key)
        for key in ("message_id", "from_user_id", "to_user_id", "group_id", "item_list")
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    evidence = TransportEvidence(
        account, conversation, SenderClaim(sender), message_id, message_id, generation, digest, now
    )
    return CanonicalInboundEvent(evidence, tuple(segments), tuple(tickets))
