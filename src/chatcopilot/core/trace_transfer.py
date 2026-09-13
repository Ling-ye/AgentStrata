"""Bounded JSON frames for a completed local trace, without a second protocol."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Iterator

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.trace_archive import TraceArchive, metadata
from chatcopilot.core.trace_capture import TraceCapture

CHUNK_BYTES = 192 * 1024


def frames(capture: TraceCapture, status: str) -> Iterator[dict[str, Any]]:
    # The supervising Core validates with the SDK after every Trial descendant
    # has exited. Do not import a full evaluation SDK in each short-lived child.
    trace = capture.finish(status, validate=False)
    files = {**capture.artifacts, "trace": json_text(trace).encode()}
    for key, raw in files.items():
        for offset in range(0, len(raw), CHUNK_BYTES):
            yield {"file": key, "offset": offset, "size": len(raw),
                   "data": base64.b64encode(raw[offset:offset + CHUNK_BYTES]).decode()}
    yield {"complete": capture.ref}


class TraceReceiver:
    def __init__(self, source: dict[str, Any]) -> None:
        self.source = source
        self.files: dict[str, bytearray] = {}
        self.sizes: dict[str, int] = {}
        self.complete = False

    def receive(self, frame: dict[str, Any]) -> None:
        if self.complete:
            raise ValueError("Trace transfer already completed")
        if set(frame) == {"complete"}:
            if "trace" not in self.files or any(len(raw) != self.sizes[key] for key, raw in self.files.items()):
                raise ValueError("Incomplete trace transfer")
            trace = json.loads(self.files["trace"])
            if metadata(trace)["source"] != self.source or trace["uuid"] != frame["complete"]:
                raise ValueError("Trace transfer source mismatch")
            self.complete = True
            return
        if set(frame) != {"file", "offset", "size", "data"}:
            raise ValueError("Malformed trace chunk")
        key, offset, size = frame["file"], frame["offset"], frame["size"]
        if (not isinstance(key, str) or (key != "trace" and (len(key) != 64 or any(c not in "0123456789abcdef" for c in key)))
                or type(offset) is not int or type(size) is not int or not 0 <= offset < size):
            raise ValueError("Invalid trace chunk identity")
        raw = base64.b64decode(frame["data"], validate=True)
        current = self.files.setdefault(key, bytearray())
        if offset != len(current) or not 0 < len(raw) <= CHUNK_BYTES or offset + len(raw) > size:
            raise ValueError("Invalid trace chunk order")
        if self.sizes.setdefault(key, size) != size:
            raise ValueError("Trace chunk size changed")
        current.extend(raw)

    def publish(self, archive: TraceArchive) -> dict[str, Any]:
        if not self.complete:
            return {"capture_state": "not_recorded"}
        trace = json.loads(self.files.pop("trace"))
        bodies = {key: bytes(raw) for key, raw in self.files.items()}
        if any(hashlib.sha256(raw).hexdigest() != key for key, raw in bodies.items()):
            raise ValueError("Trace transfer digest mismatch")
        return archive.publish(trace, bodies)
