"""Versioned framed-JSON protocol for the local Evaluation service."""

from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path
from typing import Any, Mapping

PROTOCOL = "agentstrata.evaluation.v1"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MUTATION_OPERATIONS = frozenset(
    {
        "evaluations.start",
        "evaluations.rerun",
        "evaluations.cancel",
        "evaluations.delete",
    }
)
MUTATION_ACCEPTED = "mutation_accepted"
_HEADER = struct.Struct("!I")


class ProtocolError(ValueError):
    """Raised for malformed, truncated, or oversized protocol frames."""


def default_socket_path() -> Path:
    configured = os.environ.get("CHATCOPILOT_EVALUATION_SOCKET", "").strip()
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime and os.name != "nt" and hasattr(os, "getuid"):
        runtime = f"/run/user/{os.getuid()}"
    if not runtime:
        raise RuntimeError("CHATCOPILOT_EVALUATION_SOCKET or XDG_RUNTIME_DIR is required")
    return Path(runtime) / "agentstrata-evaluation" / "service.sock"


def send_frame(
    connection: socket.socket,
    payload: Mapping[str, Any],
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> None:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("protocol payload is not valid JSON") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("protocol frame exceeds the configured limit")
    connection.sendall(_HEADER.pack(len(encoded)) + encoded)


def recv_frame(
    connection: socket.socket,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    header = _recv_exact(connection, _HEADER.size)
    if not header:
        raise EOFError
    if len(header) != _HEADER.size:
        raise ProtocolError("protocol frame header is truncated")
    (size,) = _HEADER.unpack(header)
    if size <= 0 or size > max_bytes:
        raise ProtocolError("protocol frame size is invalid")
    encoded = _recv_exact(connection, size)
    if len(encoded) != size:
        raise ProtocolError("protocol frame body is truncated")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("protocol frame is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("protocol frame must contain a JSON object")
    return {str(key): value for key, value in payload.items()}


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MUTATION_ACCEPTED",
    "MUTATION_OPERATIONS",
    "PROTOCOL",
    "ProtocolError",
    "default_socket_path",
    "recv_frame",
    "send_frame",
]
