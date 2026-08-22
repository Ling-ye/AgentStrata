"""Authenticated backend relay from a stdio MCP adapter to the live ToolExecutor."""

from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from chatcopilot.agent.trace import TraceContext, reset_trace, set_trace
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.agent import AgentEvent
from chatcopilot.contracts.tools import ToolDef

if TYPE_CHECKING:
    from chatcopilot.agent.session import ToolPayloadFilter


_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_BUFFERED_TOOL_EVENTS = 1024
_GENERIC_TOOL_FAILURE = {
    "ok": False,
    "error": "tool execution failed",
    "error_code": "tool_execution_failed",
}
_MAX_OMITTED_EVENT_COUNT = (1 << 63) - 1


@dataclass(frozen=True)
class RelayEndpoint:
    host: str
    port: int
    token: str

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "token": self.token}


@dataclass(frozen=True)
class _RelayTraceBinding:
    generation: int
    trace_id: str
    parent_span_id: str
    depth: int


@dataclass(frozen=True)
class _BufferedRelayEvent:
    generation: int | None
    payload: dict[str, Any]


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
        self._events: list[_BufferedRelayEvent] = []
        self._active_calls: dict[str, dict[str, Any]] = {}
        self._nested_event_drops: dict[tuple[int, str], dict[str, Any]] = {}
        self._abandoned_call_ids: set[str] = set()
        self._generation = 0
        self._trace_binding: _RelayTraceBinding | None = None
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

    def begin_turn(self, *, trace_id: str, parent_span_id: str, depth: int) -> int:
        """Bind one relay generation to the turn that owns its event stream."""

        normalized_trace = str(trace_id or "").strip()
        normalized_parent = str(parent_span_id or "").strip()
        if not normalized_trace or not normalized_parent:
            raise ValueError("session relay turn requires trace and parent span identity")
        if depth < 0:
            raise ValueError("session relay turn depth cannot be negative")
        with self._event_lock:
            if self._trace_binding is not None:
                raise RuntimeError("session relay already has an active turn")
            if self._events or self._active_calls or self._nested_event_drops:
                raise RuntimeError("session relay retained evidence from a prior turn")
            self._generation += 1
            self._trace_binding = _RelayTraceBinding(
                generation=self._generation,
                trace_id=normalized_trace,
                parent_span_id=normalized_parent,
                depth=depth,
            )
            return self._generation

    def end_turn(self, generation: int) -> None:
        """Retire a generation without allowing late handler output into the next turn."""

        with self._event_lock:
            self._events = [item for item in self._events if item.generation != generation]
            for call_id, active in tuple(self._active_calls.items()):
                if active.get("generation") != generation:
                    continue
                self._active_calls.pop(call_id, None)
                self._abandoned_call_ids.add(call_id)
            for key in tuple(self._nested_event_drops):
                if key[0] == generation:
                    self._nested_event_drops.pop(key, None)
            if self._trace_binding is not None and self._trace_binding.generation == generation:
                self._trace_binding = None

    def drain_tool_events(
        self, *, generation: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return one complete, bounded relay audit batch and clear the buffer."""

        with self._event_lock:
            if generation is None and self._trace_binding is not None:
                raise RuntimeError("session relay still has an active turn")
            if self._has_active_calls(generation):
                raise RuntimeError("session relay still has an active tool call")
            return self._drain_events_with_ready_omissions(generation)

    def drain_available_tool_events(
        self, *, generation: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return events recorded so far without requiring active calls to finish."""

        with self._event_lock:
            return self._drain_events_with_ready_omissions(generation)

    def drain_tool_events_with_unknown_active(
        self, *, generation: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Close active calls as unknown before retiring a failed relay generation."""

        with self._event_lock:
            events = list(
                self._drain_events_with_ready_omissions(
                    generation,
                    include_active_omissions=True,
                )
            )
            finished_at = time.time()
            for call_id, active in tuple(self._active_calls.items()):
                if generation is not None and active.get("generation") != generation:
                    continue
                events.append(
                    {
                        "type": "tool_finished",
                        "generation": active.get("generation"),
                        "call_id": call_id,
                        "name": str(active.get("name") or "unknown_tool"),
                        "trace_id": active.get("trace_id"),
                        "parent_span_id": active.get("parent_span_id"),
                        "depth": active.get("depth", 0),
                        "ok": False,
                        "summary": (
                            "Tool outcome is unknown because the Codex turn ended while "
                            "execution was still active; a late side effect remains possible."
                        ),
                        "error": "outcome_unknown_late_completion",
                        "data": {
                            "outcome": "unknown",
                            "late_completion_possible": True,
                        },
                        "finished_at": finished_at,
                    }
                )
                self._abandoned_call_ids.add(call_id)
                self._active_calls.pop(call_id, None)
            return tuple(events)

    def _record_tool_started(
        self, *, call_id: str, name: str, arguments: dict[str, Any]
    ) -> _RelayTraceBinding | None:
        with self._event_lock:
            required_slots = len(self._events) + len(self._active_calls) + 2
            if required_slots > _MAX_BUFFERED_TOOL_EVENTS:
                raise RuntimeError("session relay tool audit buffer is full")
            binding = self._trace_binding
            generation = binding.generation if binding is not None else None
            payload = {
                "type": "tool_started",
                "generation": generation,
                "call_id": call_id,
                "name": name,
                "arguments": dict(arguments),
                "started_at": time.time(),
                "trace_id": binding.trace_id if binding is not None else None,
                "parent_span_id": (
                    binding.parent_span_id if binding is not None else None
                ),
                "depth": binding.depth if binding is not None else 0,
            }
            self._events.append(_BufferedRelayEvent(generation=generation, payload=payload))
            self._active_calls[call_id] = {
                "name": name,
                "started_at": payload["started_at"],
                "generation": generation,
                "trace_id": binding.trace_id if binding is not None else None,
                "parent_span_id": (
                    binding.parent_span_id if binding is not None else None
                ),
                "depth": binding.depth if binding is not None else 0,
            }
            return binding

    def _record_nested_event(
        self,
        *,
        generation: int,
        call_id: str,
        event: AgentEvent,
    ) -> None:
        with self._event_lock:
            binding = self._trace_binding
            active = self._active_calls.get(call_id)
            if (
                binding is None
                or binding.generation != generation
                or active is None
                or active.get("generation") != generation
            ):
                return
            required_slots = len(self._events) + len(self._active_calls) + 1
            if required_slots > _MAX_BUFFERED_TOOL_EVENTS:
                key = (generation, call_id)
                dropped = self._nested_event_drops.get(key)
                if dropped is None:
                    dropped = {
                        "type": "nested_event_omission",
                        "generation": generation,
                        "call_id": call_id,
                        "name": str(active.get("name") or "unknown_tool"),
                        "trace_id": active.get("trace_id"),
                        "parent_span_id": active.get("parent_span_id"),
                        "depth": active.get("depth", 0),
                        "buffer_limit": _MAX_BUFFERED_TOOL_EVENTS,
                        "omitted_count": 0,
                    }
                    self._nested_event_drops[key] = dropped
                dropped["omitted_count"] = min(
                    _MAX_OMITTED_EVENT_COUNT,
                    int(dropped["omitted_count"]) + 1,
                )
                return
            event_trace_id = getattr(event, "trace_id", None)
            if event_trace_id is not None and event_trace_id != binding.trace_id:
                raise RuntimeError("session relay nested event trace identity drifted")
            self._events.append(
                _BufferedRelayEvent(
                    generation=generation,
                    payload={
                        "type": "agent_event",
                        "generation": generation,
                        "call_id": call_id,
                        "event": event,
                    },
                )
            )

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
            if call_id in self._abandoned_call_ids:
                self._abandoned_call_ids.remove(call_id)
                return
            active = self._active_calls.get(call_id)
            if active is None:
                raise RuntimeError("session relay tool audit completion is unpaired")
            generation = active.get("generation")
            self._events.append(
                _BufferedRelayEvent(
                    generation=generation,
                    payload={
                        "type": "tool_finished",
                        "generation": generation,
                        "call_id": call_id,
                        "name": name,
                        "trace_id": active.get("trace_id"),
                        "parent_span_id": active.get("parent_span_id"),
                        "depth": active.get("depth", 0),
                        "ok": ok,
                        "summary": summary,
                        "error": error,
                        "data": dict(data) if data is not None else None,
                        "finished_at": time.time(),
                    },
                )
            )
            self._active_calls.pop(call_id, None)

    def _has_active_calls(self, generation: int | None) -> bool:
        if generation is None:
            return bool(self._active_calls)
        return any(
            active.get("generation") == generation
            for active in self._active_calls.values()
        )

    def _drain_buffered_events(
        self, generation: int | None
    ) -> tuple[dict[str, Any], ...]:
        if generation is None:
            events = tuple(dict(item.payload) for item in self._events)
            self._events.clear()
            return events
        selected: list[dict[str, Any]] = []
        retained: list[_BufferedRelayEvent] = []
        for item in self._events:
            if item.generation == generation:
                selected.append(dict(item.payload))
            else:
                retained.append(item)
        self._events = retained
        return tuple(selected)

    def _drain_nested_event_omissions(
        self,
        generation: int | None,
        *,
        include_active: bool,
    ) -> tuple[dict[str, Any], ...]:
        selected: list[dict[str, Any]] = []
        for key, payload in tuple(self._nested_event_drops.items()):
            if generation is not None and key[0] != generation:
                continue
            if not include_active and key[1] in self._active_calls:
                continue
            selected.append(dict(payload))
            self._nested_event_drops.pop(key, None)
        return tuple(selected)

    def _drain_events_with_ready_omissions(
        self,
        generation: int | None,
        *,
        include_active_omissions: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        events = self._drain_buffered_events(generation)
        omissions = self._drain_nested_event_omissions(
            generation,
            include_active=include_active_omissions,
        )
        if not omissions:
            return events
        omissions_by_call = {
            str(item.get("call_id") or ""): item for item in omissions
        }
        ordered: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") == "tool_finished":
                call_id = str(event.get("call_id") or "")
                omission = omissions_by_call.pop(call_id, None)
                if omission is not None:
                    ordered.append(omission)
            ordered.append(event)
        ordered.extend(omissions_by_call.values())
        return tuple(ordered)

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
        binding = self._record_tool_started(call_id=call_id, name=name, arguments=arguments)
        trace_token = None
        if binding is not None:

            def nested_sink(event: AgentEvent) -> None:
                self._record_nested_event(
                    generation=binding.generation,
                    call_id=call_id,
                    event=event,
                )

            trace_token = set_trace(
                TraceContext(
                    trace_id=binding.trace_id,
                    span_id=call_id,
                    depth=binding.depth,
                    sink=nested_sink,
                )
            )
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
        finally:
            if trace_token is not None:
                reset_trace(trace_token)
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
