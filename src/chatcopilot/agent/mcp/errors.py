"""MCP client exception and timeout classification helpers."""
from __future__ import annotations

import asyncio

_TIMEOUT_KEYWORDS = ("timed out", "timeout", "deadline exceeded")

class McpToolTimeoutError(RuntimeError):
    """Raised when a specific MCP tool call exceeds its deadline.

    Unlike a generic RuntimeError, this signals a transient issue with one
    call rather than a broken server connection.
    """

    def __init__(
        self, msg: str, *, server_id: str, tool_name: str, timeout_seconds: float
    ) -> None:
        super().__init__(msg)
        self.server_id = server_id
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds


class McpToolBusyError(RuntimeError):
    """Raised when a tool call is rejected because an in-flight call already
    occupies the same single-concurrency MCP server (e.g. browser-backed)."""

    def __init__(self, msg: str, *, server_id: str, tool_name: str) -> None:
        super().__init__(msg)
        self.server_id = server_id
        self.tool_name = tool_name


def _is_timeout_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in _TIMEOUT_KEYWORDS)


__all__ = ["McpToolBusyError", "McpToolTimeoutError", "_is_timeout_error"]
