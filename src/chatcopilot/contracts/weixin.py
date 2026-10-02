"""Protocol values and pure validation; no environment or network access."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import math
from urllib.parse import urlsplit
from typing import Protocol


class WeixinApiClientPort(Protocol):
    base_url: str

    async def request(
        self,
        endpoint: str,
        body: dict | None = None,
        *,
        timeout: float = 45,
        authenticated: bool = True,
        method: str = "POST",
    ) -> dict: ...

    async def close(self) -> None: ...


class WeixinMediaClientPort(Protocol):
    async def download(self, url: str, *, max_bytes: int) -> bytes: ...


API_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


class WeixinError(RuntimeError):
    """Secret-free transport error, safe to expose as a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(c) < 33 or c in ",/\\" for c in value)
    ):
        raise WeixinError("weixin_identity_invalid")
    return value


def https_url(value: str) -> str:
    """Only protocol-owned HTTPS hosts; redirects are validated separately."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid = (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and host.endswith(".weixin.qq.com")
        )
    except (TypeError, ValueError):
        valid = False
    if not valid or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise WeixinError("weixin_url_not_allowed")
    return value


def media_key(value: str, *, hexadecimal: bool = False) -> bytes:
    if not isinstance(value, str):
        raise WeixinError("weixin_media_key_invalid")
    try:
        raw = bytes.fromhex(value) if hexadecimal else base64.b64decode(value, validate=True)
        if not hexadecimal and len(raw) == 32:
            raw = bytes.fromhex(raw.decode("ascii"))
        if len(raw) != 16:
            raise ValueError("invalid key length")
        return raw
    except (ValueError, UnicodeError):
        raise WeixinError("weixin_media_key_invalid") from None


@dataclass(frozen=True)
class WeixinChannelConfig:
    account_id: str
    user_id: str
    token: str = field(repr=False, metadata={"secret": True})
    base_url: str = API_BASE
    channel_id: str = "weixin"
    action_timeout_seconds: float = 120.0
    long_poll_timeout_seconds: float = 35.0
    max_frame_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        identity(self.account_id)
        identity(self.user_id)
        if not self.token or any(ord(c) < 33 for c in self.token):
            raise WeixinError("weixin_token_invalid")
        https_url(self.base_url)
        if urlsplit(self.base_url).path not in ("", "/") or urlsplit(self.base_url).query:
            raise WeixinError("weixin_base_url_invalid")
        if self.channel_id != "weixin":
            raise WeixinError("weixin_channel_id_invalid")
        for value in (self.action_timeout_seconds, self.long_poll_timeout_seconds):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise WeixinError("weixin_timeout_invalid")
        if (
            type(self.max_frame_bytes) is not int
            or not 1024 <= self.max_frame_bytes <= 16 * 1024 * 1024
        ):
            raise WeixinError("weixin_frame_limit_invalid")
