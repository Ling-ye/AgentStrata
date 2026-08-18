"""Authenticated backend relay from a stdio MCP adapter to the live ToolExecutor."""

from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.external_tools.shared.tool_spec import ToolDef

if TYPE_CHECKING:
    from chatcopilot.agent.session import ToolPayloadFilter


_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_BUFFERED_TOOL_EVENTS = 1024
_GENERIC_TOOL_FAILURE = {
    "ok": False,
    "error": "tool execution failed",
    "error_code": "tool_execution_failed",
}


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

    def __init__(
        self,
        *,
        tools: Sequence[ToolDef],
        executor: ToolExecutor,
        payload_filter: ToolPayloadFilter | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._executor = executor
        self._payload_filter = payload_filter
        self._token = secrets.token_urlsafe(32)
        self._event_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._reserved_finishes = 0
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
                except Exception:  # noqa: BLE001 - never expose relay internals
                    response = {
                        "ok": False,
                        "error": "relay request failed",
                        "error_code": "relay_failed",
                    }
                self._write(response)

            def _write(self, payload: dict[str, Any]) -> None:
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")

        self._server = _RelayServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="chatcopilot-session-tool-relay",
            daemon=True,
        )

    def start(self) -> RelayEndpoint:
        self._thread.start()
        address = self._server.server_address
        host, port = address[0], address[1]
        return RelayEndpoint(str(host), int(port), self._token)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def drain_tool_events(self) -> tuple[dict[str, Any], ...]:
        """Return one complete, bounded relay audit batch and clear the buffer."""

        with self._event_lock:
            if self._reserved_finishes:
                raise RuntimeError("session relay still has an active tool call")
            events = tuple(dict(item) for item in self._events)
            self._events.clear()
            return events

    def _record_tool_started(self, *, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        with self._event_lock:
            required_slots = len(self._events) + self._reserved_finishes + 2
            if required_slots > _MAX_BUFFERED_TOOL_EVENTS:
                raise RuntimeError("session relay tool audit buffer is full")
            self._events.append(
                {
                    "type": "tool_started",
                    "call_id": call_id,
                    "name": name,
                    "arguments": dict(arguments),
                }
            )
            self._reserved_finishes += 1

    def _record_tool_finished(
        self,
        *,
        call_id: str,
        name: str,
        ok: bool,
        summary: str,
        error: str | None,
        data: dict[str, Any] | None,
    ) -> None:
        with self._event_lock:
            if self._reserved_finishes <= 0:
                raise RuntimeError("session relay tool audit completion is unpaired")
            self._events.append(
                {
                    "type": "tool_finished",
                    "call_id": call_id,
                    "name": name,
                    "ok": ok,
                    "summary": summary,
                    "error": error,
                    "data": dict(data) if data is not None else None,
                }
            )
            self._reserved_finishes -= 1

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
        call_id = secrets.token_hex(16)
        self._record_tool_started(call_id=call_id, name=name, arguments=arguments)
        try:
            result = self._executor.execute(name, arguments)
            result_payload = result.to_llm_payload()
            if self._payload_filter is not None:
                result_payload = self._payload_filter(dict(result_payload))
            if not isinstance(result_payload, dict):
                raise TypeError("tool payload filter must return an object")
            # Fail here, before the payload reaches either output channel, when a
            # trusted filter accidentally returns a non-JSON value.
            json.dumps(result_payload, ensure_ascii=False, allow_nan=False)
        except Exception:  # noqa: BLE001 - relay failures are deliberately opaque
            result_payload = dict(_GENERIC_TOOL_FAILURE)
        result_ok = result_payload.get("ok") is True
        summary = str(result_payload.get("summary") or "") if result_ok else ""
        error = None if result_ok else str(
            result_payload.get("error") or _GENERIC_TOOL_FAILURE["error"]
        )
        self._record_tool_finished(
            call_id=call_id,
            name=name,
            ok=result_ok,
            summary=summary,
            error=error,
            data=result_payload,
        )
        return {"ok": True, "result": {"tool": name, **result_payload}}


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
