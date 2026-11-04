"""Cross-process concurrency guards for MCP tool calls."""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from chatcopilot.agent.mcp.errors import McpToolBusyError
from chatcopilot.core import file_lock as fcntl

_MCP_LOCK_DIR = Path("/tmp") if sys.platform != "win32" else Path(os.environ.get("TEMP", "."))


@contextmanager
def _cross_process_lock(server_id: str, timeout: float) -> Iterator[None]:
    """Acquire a real interprocess advisory lock on POSIX and Windows.

    Blocks up to *timeout* seconds; raises ``McpToolBusyError`` if the lock
    cannot be obtained within the deadline.
    """
    lock_path = _MCP_LOCK_DIR / f"chatcopilot-mcp-lock-{server_id}.lock"
    if os.name == "nt":
        from chatcopilot.core.private_sqlite import private_directory
        private_directory(_MCP_LOCK_DIR / "agentstrata-mcp-locks")
        lock_path = _MCP_LOCK_DIR / "agentstrata-mcp-locks" / lock_path.name
    deadline = time.monotonic() + timeout
    fd = lock_path.open("w")
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
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


__all__ = ["_cross_process_lock"]
