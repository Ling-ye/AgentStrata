from __future__ import annotations

from fastapi import APIRouter, HTTPException

from console.backend.sse import sse
from console.control import operations

router = APIRouter(prefix="/api/console", tags=["console"])


@router.post("/update")
def console_update():
    res = operations.trigger_console_update()
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error") or "console update failed")
    return res


@router.get("/logs/stream")
def stream_console_logs(lines: int = 200):
    error = operations.console_log_error()
    if error:
        raise HTTPException(status_code=503, detail=error)
    return sse(operations.follow_console_log(from_end_lines=lines))
