"""Cross-process concurrency guards for MCP tool calls."""
from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from chatcopilot.agent.mcp.errors import McpToolBusyError

_MCP_LOCK_DIR = Path("/tmp") if sys.platform != "win32" else Path(os.environ.get("TEMP", "."))
_IS_UNIX = sys.platform != "win32"
_WIN_FALLBACK_LOCKS: dict[str, threading.Lock] = {}
_WIN_FALLBACK_META_LOCK = threading.Lock()


@contextmanager
def _cross_process_lock(server_id: str, timeout: float) -> Iterator[None]:
    """Acquire a cross-process advisory lock (Unix fcntl / Windows fallback).

    Blocks up to *timeout* seconds; raises ``McpToolBusyError`` if the lock
    cannot be obtained within the deadline.
    """
    if _IS_UNIX:
        yield from _unix_flock(server_id, timeout)
    else:
        yield from _win_thread_lock(server_id, timeout)


def _unix_flock(server_id: str, timeout: float) -> Iterator[None]:
    import fcntl

    lock_path = _MCP_LOCK_DIR / f"chatcopilot-mcp-lock-{server_id}.lock"
    deadline = time.monotonic() + timeout
    fd = lock_path.open("w")
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise McpToolBusyError(
                        f"MCP server '{server_id}' concurrency lock timed out "
                        f"after {timeout:.0f}s; another call is still in flight.",
                        server_id=server_id,
                        tool_name="",
                    )
                time.sleep(0.25)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def _win_thread_lock(server_id: str, timeout: float) -> Iterator[None]:
    with _WIN_FALLBACK_META_LOCK:
        lock = _WIN_FALLBACK_LOCKS.setdefault(server_id, threading.Lock())
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise McpToolBusyError(
            f"MCP server '{server_id}' concurrency lock timed out "
            f"after {timeout:.0f}s; another call is still in flight.",
            server_id=server_id,
            tool_name="",
        )
    try:
        yield
    finally:
        lock.release()


__all__ = ["_cross_process_lock"]
