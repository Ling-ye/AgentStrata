"""Identity and role contracts shared across layers."""
from __future__ import annotations

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
    "Identity",
    "Role",
    "SessionIdentity",
    "role_ge",
    "role_value",
]
