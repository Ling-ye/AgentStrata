"""Cooperative shutdown for a native foreground deployment's owned children."""
from __future__ import annotations

import asyncio
import os

STOP_EVENT = "AGENTSTRATA_PROCESS_STOP_EVENT"


async def watch_shutdown(callback) -> None:
    name = os.environ.get(STOP_EVENT)
    if os.name != "nt" or not name:
        return
    import win32con
    import win32event
    handle = win32event.OpenEvent(win32con.SYNCHRONIZE, False, name)
    try:
        while win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT:
            await asyncio.sleep(0.1)
        callback()
    finally:
        handle.Close()
