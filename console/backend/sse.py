from __future__ import annotations

import json
from typing import Iterator

from fastapi.responses import StreamingResponse

from console.control import operations


def sse(gen: Iterator[str]) -> StreamingResponse:
    def event_stream() -> Iterator[bytes]:
        for line in gen:
            if line == operations.KEEPALIVE:
                yield b": ping\n\n"
                continue
            payload = json.dumps({"line": line}, ensure_ascii=False)
            yield f"data: {payload}\n\n".encode("utf-8")
        yield b"event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
