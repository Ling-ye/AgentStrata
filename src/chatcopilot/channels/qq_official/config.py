"""Validated settings for the official QQ Bot API (not OneBot)."""
from dataclasses import dataclass, field
import re


class QQOfficialError(ValueError):
    """Secret-free provider error."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class QQOfficialChannelConfig:
    channel_id: str
    app_id: str
    app_secret: str = field(repr=False)
    action_timeout_seconds: float = 20.0
    max_pending_events: int = 256

    @property
    def account_id(self) -> str:
        return self.app_id

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.channel_id):
            raise QQOfficialError("qq_official_channel_invalid")
        if not re.fullmatch(r"[0-9]{1,32}", self.app_id):
            raise QQOfficialError("qq_official_app_id_invalid")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip() or len(self.app_secret) > 512:
            raise QQOfficialError("qq_official_secret_missing")
        if (type(self.action_timeout_seconds) not in (int, float)
                or not 1 <= self.action_timeout_seconds <= 120):
            raise QQOfficialError("qq_official_timeout_invalid")
        if type(self.max_pending_events) is not int or not 1 <= self.max_pending_events <= 1024:
            raise QQOfficialError("qq_official_queue_invalid")


