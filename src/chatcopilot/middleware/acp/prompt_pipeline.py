"""Small prompt-turn helpers for ACP session/prompt handling."""
from __future__ import annotations

from typing import Any


def looks_like_quoted_reply(text: str) -> bool:
    """Return a platform-neutral hint that this prompt may be replying to prior text."""
    lowered = (text or "").strip().lower()
    return any(marker in lowered for marker in ("[引用]", "引用：", "回复：", "reply:", "quote:"))


def build_topic_metadata(
    *,
    user_text: str,
    chat_kind: str | None,
    has_attachment: bool,
    message_count: int,
) -> dict[str, Any]:
    return {
        "chat_kind": chat_kind or "",
        "has_attachment": bool(has_attachment),
        "has_quote": looks_like_quoted_reply(user_text),
        "message_count": message_count,
    }


__all__ = ["build_topic_metadata", "looks_like_quoted_reply"]
