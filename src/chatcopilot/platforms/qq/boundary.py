"""QQ token and loopback endpoint validation shared by gateway components."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class QQBoundaryError(ValueError):
    """Stable diagnostic that never includes the rejected secret value."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def require_access_token(token: str | None) -> str:
    value = str(token or "").strip()
    if not value:
        raise QQBoundaryError(
            "qq_access_token_missing",
            "QQ_ACCESS_TOKEN is required",
        )
    if _TOKEN_RE.fullmatch(value) is None:
        raise QQBoundaryError(
            "qq_access_token_invalid",
            "QQ_ACCESS_TOKEN must be 32-128 URL-safe characters",
        )
    return value


def require_loopback_websocket_url(
    value: str | None,
    *,
    env_key: str,
) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise QQBoundaryError(
            "qq_websocket_url_invalid",
            f"{env_key} must be a valid loopback WebSocket URL",
        ) from exc
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is None
    ):
        raise QQBoundaryError(
            "qq_websocket_url_not_loopback",
            f"{env_key} must use ws/wss on localhost, 127.0.0.1, or ::1 with an explicit port",
        )
    return url


__all__ = [
    "QQBoundaryError",
    "require_access_token",
    "require_loopback_websocket_url",
]
