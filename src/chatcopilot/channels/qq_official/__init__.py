"""Official QQ Bot channel, separate from the personal-QQ OneBot transport."""
from .config import QQOfficialChannelConfig, QQOfficialError
from .driver import QQOfficialWebSocketDriver

__all__ = ["QQOfficialChannelConfig", "QQOfficialWebSocketDriver", "QQOfficialError"]


