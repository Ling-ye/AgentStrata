"""Bounded native message projection without authentication or resource locators."""

from __future__ import annotations

from typing import Any

from chatcopilot.contracts.gateway import ChannelInputSegment, ResourceTicket


_MAX_TEXT_BYTES = 16 * 1024
_MAX_SEGMENTS = 64
_RESOURCE_KINDS = frozenset({"image", "record", "audio", "video", "file"})


def project_message(
    message: Any, tickets: tuple[ResourceTicket, ...],
) -> tuple[tuple[ChannelInputSegment, ...], bool]:
    native = [{"type": "text", "data": {"text": message}}] if isinstance(message, str) else message
    resources = iter(tickets)
    remaining = _MAX_TEXT_BYTES
    truncated = len(native) > _MAX_SEGMENTS
    result = []
    for item in native[:_MAX_SEGMENTS]:
        kind = item["type"].strip().lower()
        data = item["data"]
        if kind == "text":
            raw = data["text"].encode("utf-8")
            selected = raw[:remaining].decode("utf-8", "ignore")
            truncated |= len(raw) > remaining
            remaining -= len(selected.encode("utf-8"))
            result.append(ChannelInputSegment(kind="text", text=selected))
        elif kind in {"at", "reply"}:
            target = str(data["qq" if kind == "at" else "id"])
            result.append(ChannelInputSegment(kind=kind, target=target[:256]))
        elif kind in _RESOURCE_KINDS:
            ticket = next(resources)
            result.append(ChannelInputSegment(kind=kind, name=ticket.name, size_bytes=ticket.size_bytes))
        else:
            result.append(ChannelInputSegment(kind="unknown"))
    return tuple(result), truncated
