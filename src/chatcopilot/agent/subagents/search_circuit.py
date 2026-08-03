"""Search-source circuit breaker shared by subagent and direct-search paths."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

_SEARCH_FAILURE_TTLS = {
    "mcp_quota_exceeded": 3600.0,  # base; escalates via _QUOTA_MAX_TTL
    "mcp_unavailable": 120.0,
    "mcp_timeout": 120.0,
    "mcp_busy": 120.0,
}
_QUOTA_MAX_TTL = float(os.environ.get("CHATCOPILOT_SEARCH_QUOTA_MAX_TTL", 86400))


class SearchCircuitBreaker:
    """Process-local search-source health memory shared by sessions in one runtime.

    Quota failures (``mcp_quota_exceeded``) use **escalating TTL**: each
    consecutive quota failure for the same server doubles the block duration up
    to ``_QUOTA_MAX_TTL`` (default 24 h, env-overridable).  A single success
    resets both the block and the strike counter.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[float, str]] = {}
        self._quota_strikes: dict[str, int] = {}
        self._lock = threading.Lock()

    def blocked(self, server_id: str) -> str | None:
        with self._lock:
            entry = self._entries.get(server_id)
            if entry is None:
                return None
            until, error_code = entry
            if self._clock() >= until:
                self._entries.pop(server_id, None)
                return None
            return error_code

    def record_failure(self, server_id: str, error_code: str | None) -> None:
        base_ttl = _SEARCH_FAILURE_TTLS.get(str(error_code or ""))
        if base_ttl is None:
            return
        with self._lock:
            if error_code == "mcp_quota_exceeded":
                strikes = self._quota_strikes.get(server_id, 0) + 1
                self._quota_strikes[server_id] = strikes
                ttl = min(base_ttl * (2 ** (strikes - 1)), _QUOTA_MAX_TTL)
            else:
                ttl = base_ttl
            self._entries[server_id] = (self._clock() + ttl, str(error_code))

    def record_success(self, server_id: str) -> None:
        with self._lock:
            self._entries.pop(server_id, None)
            self._quota_strikes.pop(server_id, None)


__all__ = ["SearchCircuitBreaker", "_SEARCH_FAILURE_TTLS"]
