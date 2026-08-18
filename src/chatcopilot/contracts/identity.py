"""Identity and role contracts shared across layers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Role(str, Enum):
    """Three-level role model used by permissions and prompt assembly."""

    OWNER = "owner"
    ADMIN = "admin"
    USER = "user"


class AssistantMode(str, Enum):
    """Business capability mode selected by session-level policy."""

    PERFORMANCE = "performance"
    GENERAL = "general"


@dataclass(frozen=True)
class Identity:
    """Configured identity entry used by role resolution."""

    name: Optional[str] = None
    user_id: Optional[str] = None

    def is_valid(self) -> bool:
        return bool((self.name or "").strip()) or bool((self.user_id or "").strip())

    def matches(self, *, user_id: Optional[str], user_name: Optional[str]) -> bool:
        my_id = (self.user_id or "").strip()
        if my_id and user_id and my_id == user_id.strip():
            return True
        my_name = (self.name or "").strip().casefold()
        in_name = (user_name or "").strip().casefold()
        return bool(my_name and in_name and my_name == in_name)


@dataclass(frozen=True)
class SessionIdentity:
    """Normalized chat identity parsed from platform session metadata."""

    user_id: str | None = None
    chat_id: str | None = None
    chat_kind: str | None = None
    user_name: str | None = None


@dataclass(frozen=True)
class ConversationIdentity:
    """Stable conversation scope, independent from the current speaker."""

    platform: str
    chat_kind: str
    chat_id: str


@dataclass(frozen=True)
class TurnIdentity:
    """Authenticated source metadata for one inbound conversation turn."""

    conversation: ConversationIdentity
    sender_user_id: str
    sender_user_name: str | None = None
    message_id: str | None = None
    source: str = "cc-connect"

    @property
    def actor_ref(self) -> str:
        return stable_actor_ref(
            self.conversation.platform,
            self.sender_user_id,
            conversation_id=(
                f"{self.conversation.chat_kind}:{self.conversation.chat_id}"
            ),
        )


def stable_actor_ref(
    platform: str,
    user_id: str,
    *,
    conversation_id: str = "",
) -> str:
    """Return a conversation-scoped pseudonym for display, never authorization."""

    normalized_platform = (platform or "chat").strip().lower() or "chat"
    normalized_user = (user_id or "").strip()
    if not normalized_user:
        return f"{normalized_platform}-actor-unknown"
    normalized_conversation = (conversation_id or "").strip()
    digest = hashlib.sha256(
        f"{normalized_platform}\0{normalized_conversation}\0{normalized_user}".encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    return f"{normalized_platform}-actor-{digest}"


_ROLE_RANK = {
    Role.USER.value: 0,
    Role.ADMIN.value: 1,
    Role.OWNER.value: 2,
}


def role_value(role: object) -> str:
    value = getattr(role, "value", role)
    return str(value or "").strip().lower()


def role_ge(current: object, required: object) -> bool:
    current_rank = _ROLE_RANK.get(role_value(current), -1)
    required_rank = _ROLE_RANK.get(role_value(required), -1)
    return current_rank >= required_rank


__all__ = [
    "AssistantMode",
    "ConversationIdentity",
    "Identity",
    "Role",
    "SessionIdentity",
    "TurnIdentity",
    "role_ge",
    "role_value",
    "stable_actor_ref",
]
