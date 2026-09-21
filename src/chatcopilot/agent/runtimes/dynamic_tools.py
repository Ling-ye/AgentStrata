"""In-process App Server tools using the existing host executor and receipts."""

from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

from chatcopilot.agent.trace import TraceContext, set_trace, reset_trace
from chatcopilot.core.observability_redaction import bound_observability_payload
from chatcopilot.contracts.agent import ToolCatalogObserved, SpanFinished


class DynamicToolBridge:
    def __init__(self, *, tools, executor, payload_filter=None):
        self.tools = {tool.name: tool for tool in tools}
        self.executor = executor
        self.payload_filter = payload_filter
        self.lock = threading.RLock()
        self.events = []
        self.active = {}
        self.generation = 0
        self.binding = None
        self.dropped = {}
        self.seen = set()

    def schemas(self):
        return (
            [
                {
                    "type": "namespace",
                    "name": "agentstrata",
                    "description": "Authorized host services",
                    "tools": [
                        {
                            "type": "function",
                            "name": tool.name,
                            "description": tool.summary,
                            "inputSchema": tool.input_schema,
                            "deferLoading": tool.name
                            not in {"send_files_to_user", "persona_manage"},
                        }
                        for tool in self.tools.values()
                    ],
                }
            ]
            if self.tools
            else []
        )

    def begin_turn(self, *, trace_id, parent_span_id, depth, request_text=""):
        with self.lock:
            if self.binding is not None or self.active or self.events:
                raise RuntimeError("dynamic tools retained an unfinished turn")
            self.generation += 1
            self.dropped.clear()
            self.seen.clear()
            self.binding = dict(
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                depth=depth,
                generation=self.generation,
                request_text=request_text,
            )
            self.events.append(
                {
                    "type": "agent_event",
                    "generation": self.generation,
                    "event": ToolCatalogObserved(
                        phase="host_prepared",
                        tools=tuple(self.tools),
                        trace_id=trace_id,
                        parent_span_id=parent_span_id,
                        source="dynamic_tools",
                    ),
                }
            )
            return self.generation

    def call(self, params, *, main_thread_id, generation=None):
        if params.get("namespace") != "agentstrata" or not isinstance(
            params.get("arguments"), dict
        ):
            return self._response({"ok": False, "error_code": "invalid_dynamic_call"})
        name, args = params.get("tool"), params["arguments"]
        tool = self.tools.get(name)
        if tool is None or (
            params.get("threadId") != main_thread_id and "subagent" not in tool.audiences
        ):
            return self._response({"ok": False, "error_code": "tool_denied"})
        with self.lock:
            if self.binding is None or generation is not None and generation != self.generation:
                return self._response({"ok": False, "error_code": "turn_inactive"})
            binding = dict(self.binding)
            call_id = str(params.get("callId") or "call_" + uuid4().hex)
            if call_id in self.seen:
                return self._response(
                    {"ok": False, "error_code": "duplicate_tool_call", "retry_safe": False}
                )
            if call_id in self.active or len(self.events) + 2 * len(self.active) >= 1024:
                return self._response({"ok": False, "error_code": "tool_backpressure"})
            self.seen.add(call_id)
            event = {
                key: binding[key] for key in ("generation", "trace_id", "parent_span_id", "depth")
            }
            event.update(call_id=call_id, name=name)
            self.active[call_id] = event
            self.events.append(
                {**event, "type": "tool_started", "arguments": args, "started_at": time.time()}
            )

        def nested_sink(value):
            with self.lock:
                if self.binding and self.binding["generation"] == binding["generation"]:
                    if len(self.events) + 2 * len(self.active) < 1024:
                        self.events.append(
                            {
                                "type": "agent_event",
                                "generation": binding["generation"],
                                "event": value,
                            }
                        )
                    else:
                        self.dropped[call_id] = self.dropped.get(call_id, 0) + 1

        token = set_trace(
            TraceContext(
                trace_id=binding["trace_id"],
                span_id=call_id,
                depth=binding["depth"],
                sink=nested_sink,
            )
        )
        captured = None
        try:
            result = self.executor.execute(name, args, request_text=binding["request_text"])
            payload = result.to_llm_payload()
            captured = bound_observability_payload(payload).value
            if self.payload_filter:
                payload = self.payload_filter(payload)
            payload = self.executor.project_result(name, payload)
            json.dumps(payload, allow_nan=False)
        except Exception:
            payload = {
                "ok": False,
                "error_code": "tool_execution_failed",
                "error": "Host tool failed",
                "outcome": "unknown",
                "retry_safe": False,
            }
        finally:
            reset_trace(token)
        with self.lock:
            if call_id in self.active:
                self.active.pop(call_id)
                if self.dropped.pop(call_id, 0):
                    self.events.append(
                        {
                            "type": "agent_event",
                            "generation": binding["generation"],
                            "event": SpanFinished(
                                name="Nested tool observations truncated",
                                kind="provider_omission",
                                ok=False,
                                trace_id=binding["trace_id"],
                                span_id=call_id + "-omission",
                                parent_span_id=call_id,
                                data={"status": "truncated"},
                            ),
                        }
                    )
                self.events.append(
                    {
                        **event,
                        "type": "tool_finished",
                        "ok": payload.get("ok") is True,
                        "summary": payload.get("summary", ""),
                        "error": payload.get("error"),
                        "data": payload,
                        "execution_result": captured,
                        "finished_at": time.time(),
                    }
                )
        return self._response(payload)

    @staticmethod
    def _response(payload):
        return {
            "success": payload.get("ok") is True,
            "contentItems": [
                {"type": "inputText", "text": json.dumps(payload, ensure_ascii=False)}
            ],
        }

    def drain_available_tool_events(self, *, generation=None):
        with self.lock:
            events, self.events = tuple(self.events), []
            return events

    def drain_tool_events(self, *, generation=None):
        with self.lock:
            if self.active:
                raise RuntimeError("host tool execution is still active")
            return self.drain_available_tool_events(generation=generation)

    def drain_tool_events_with_unknown_active(self, *, generation=None):
        with self.lock:
            for event in self.active.values():
                self.events.append(
                    {
                        **event,
                        "type": "tool_finished",
                        "ok": False,
                        "summary": "Tool outcome is unknown; late completion remains possible",
                        "error": "outcome_unknown",
                        "data": {"outcome": "unknown"},
                        "finished_at": time.time(),
                    }
                )
            self.active.clear()
            return self.drain_available_tool_events(generation=generation)

    def end_turn(self, generation):
        with self.lock:
            self.binding = None

    def close(self):
        self.drain_tool_events_with_unknown_active()
        self.binding = None
