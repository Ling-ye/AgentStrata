"""Authenticated backend relay from a stdio MCP adapter to the live ToolExecutor."""
from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Any, Sequence

from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.external_tools.shared.tool_spec import ToolDef


_MAX_REQUEST_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RelayEndpoint:
    host: str
    port: int
    token: str

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "token": self.token}


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class SessionToolRelay:
    """Expose one session's already-filtered tools over loopback only."""

    def __init__(self, *, tools: Sequence[ToolDef], executor: ToolExecutor) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._executor = executor
        self._token = secrets.token_urlsafe(32)
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
                if not raw or len(raw) > _MAX_REQUEST_BYTES:
                    self._write({"ok": False, "error": "invalid relay request size"})
                    return
                try:
                    request = json.loads(raw.decode("utf-8"))
                    response = relay._dispatch(request)
                except Exception as exc:  # noqa: BLE001
                    response = {
                        "ok": False,
                        "error": f"relay request failed: {type(exc).__name__}: {exc}",
                    }
                self._write(response)

            def _write(self, payload: dict[str, Any]) -> None:
                self.wfile.write(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
                )

        self._server = _RelayServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="chatcopilot-session-tool-relay",
            daemon=True,
        )

    def start(self) -> RelayEndpoint:
        self._thread.start()
        host, port = self._server.server_address
        return RelayEndpoint(str(host), int(port), self._token)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "relay request must be an object"}
        token = str(request.get("token") or "")
        if not secrets.compare_digest(token, self._token):
            return {"ok": False, "error": "relay authentication failed"}
        action = str(request.get("action") or "")
        if action == "list_tools":
            return {
                "ok": True,
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.summary,
                        "input_schema": {
                            "type": "object",
                            "properties": tool.properties,
                            "required": tool.required,
                        },
                    }
                    for tool in self._tools.values()
                ],
            }
        if action != "call_tool":
            return {"ok": False, "error": f"unsupported relay action: {action}"}
        name = str(request.get("name") or "")
        if name not in self._tools:
            return {"ok": False, "error": "tool denied", "error_code": "tool_denied"}
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "tool arguments must be an object"}
        result = self._executor.execute(name, arguments)
        return {"ok": True, "result": {"tool": name, **result.to_llm_payload()}}


def call_session_relay(
    endpoint: dict[str, Any], payload: dict[str, Any], *, timeout_seconds: float = 30.0
) -> dict[str, Any]:
    """Make one authenticated request to a session relay."""

    request = {**payload, "token": str(endpoint.get("token") or "")}
    host = str(endpoint.get("host") or "")
    port = int(endpoint.get("port") or 0)
    if host != "127.0.0.1" or not (1 <= port <= 65535):
        raise ValueError("session relay must use a valid IPv4 loopback endpoint")
    with socket.create_connection((host, port), timeout=max(1.0, timeout_seconds)) as conn:
        stream = conn.makefile("rwb")
        stream.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        stream.flush()
        raw = stream.readline(_MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > _MAX_REQUEST_BYTES:
        raise RuntimeError("invalid session relay response size")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("session relay response must be an object")
    return response


__all__ = [
    "RelayEndpoint",
    "SessionToolRelay",
    "call_session_relay",
]
