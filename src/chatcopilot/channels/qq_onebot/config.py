"""Fail-closed configuration boundary for the personal-QQ OneBot Channel."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from urllib.parse import urlsplit


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_QQ_ID_RE = re.compile(r"^[1-9][0-9]{4,19}$")
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OneBotConfigError(ValueError):
    """Configuration rejection whose diagnostic never contains a secret value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OneBotChannelConfig:
    """Validated connection settings for one independently installed provider."""

    channel_id: str
    account_id: str
    websocket_url: str
    access_token: str = field(repr=False)
    action_timeout_seconds: float = 10.0
    max_frame_bytes: int = 256 * 1024
    max_outbound_frame_bytes: int = 8 * 1024 * 1024
    max_pending_actions: int = 64
    max_pending_events: int = 64
    resource_ticket_ttl_seconds: float = 300.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0

    def __post_init__(self) -> None:
        if _CHANNEL_ID_RE.fullmatch(self.channel_id) is None:
            raise OneBotConfigError(
                "onebot_channel_id_invalid",
                "channel_id must be 1-64 ASCII letters, numbers, dots, underscores, or dashes",
            )
        if _QQ_ID_RE.fullmatch(self.account_id) is None:
            raise OneBotConfigError(
                "onebot_account_invalid",
                "account_id must be a stable numeric QQ account",
            )
        if _TOKEN_RE.fullmatch(self.access_token) is None:
            raise OneBotConfigError(
                "onebot_access_token_invalid",
                "access_token must be 32-128 URL-safe characters",
            )
        _validate_loopback_url(self.websocket_url)
        if (
            isinstance(self.action_timeout_seconds, bool)
            or not isinstance(self.action_timeout_seconds, (int, float))
            or not math.isfinite(self.action_timeout_seconds)
            or self.action_timeout_seconds <= 0
        ):
            raise OneBotConfigError(
                "onebot_action_timeout_invalid",
                "action_timeout_seconds must be positive",
            )
        if (
            isinstance(self.max_frame_bytes, bool)
            or not isinstance(self.max_frame_bytes, int)
            or not 1024 <= self.max_frame_bytes <= 16 * 1024 * 1024
        ):
            raise OneBotConfigError(
                "onebot_frame_limit_invalid",
                "max_frame_bytes must be between 1024 and 16777216",
            )
        if (
            isinstance(self.max_outbound_frame_bytes, bool)
            or not isinstance(self.max_outbound_frame_bytes, int)
            or not 1024 <= self.max_outbound_frame_bytes <= 32 * 1024 * 1024
        ):
            raise OneBotConfigError(
                "onebot_outbound_frame_limit_invalid",
                "max_outbound_frame_bytes must be between 1024 and 33554432",
            )
        if (
            isinstance(self.max_pending_actions, bool)
            or not isinstance(self.max_pending_actions, int)
            or not 1 <= self.max_pending_actions <= 1024
        ):
            raise OneBotConfigError(
                "onebot_pending_limit_invalid",
                "max_pending_actions must be between 1 and 1024",
            )
        if (
            isinstance(self.max_pending_events, bool)
            or not isinstance(self.max_pending_events, int)
            or not 1 <= self.max_pending_events <= 1024
        ):
            raise OneBotConfigError(
                "onebot_event_queue_limit_invalid",
                "max_pending_events must be between 1 and 1024",
            )
        if (
            isinstance(self.resource_ticket_ttl_seconds, bool)
            or not isinstance(self.resource_ticket_ttl_seconds, (int, float))
            or not math.isfinite(self.resource_ticket_ttl_seconds)
            or not 1 <= self.resource_ticket_ttl_seconds <= 3600
        ):
            raise OneBotConfigError(
                "onebot_resource_ttl_invalid",
                "resource_ticket_ttl_seconds must be between 1 and 3600",
            )
        for value, code, label in (
            (
                self.reconnect_initial_seconds,
                "onebot_reconnect_initial_invalid",
                "reconnect_initial_seconds",
            ),
            (
                self.reconnect_max_seconds,
                "onebot_reconnect_max_invalid",
                "reconnect_max_seconds",
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                or value > 300
            ):
                raise OneBotConfigError(
                    code,
                    f"{label} must be greater than 0 and at most 300 seconds",
                )
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise OneBotConfigError(
                "onebot_reconnect_range_invalid",
                "reconnect_max_seconds cannot be smaller than reconnect_initial_seconds",
            )


def _validate_loopback_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OneBotConfigError(
            "onebot_websocket_url_invalid",
            "websocket_url must be a valid loopback WebSocket URL",
        ) from exc
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or port <= 0
        or parsed.query
        or parsed.fragment
    ):
        raise OneBotConfigError(
            "onebot_websocket_url_not_loopback",
            "websocket_url must use ws/wss on an explicit loopback port without URL credentials",
        )


__all__ = ["OneBotChannelConfig", "OneBotConfigError"]
