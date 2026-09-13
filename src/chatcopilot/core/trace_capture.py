"""Task-local snapshots of existing observations, independent of their owner."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import logging
import threading
import time
from typing import Any, Iterator

from chatcopilot.core.agent_process import AgentProcessAdapter
from chatcopilot.core.observability_redaction import redact_observability_payload, default_observability_roots
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.trace_codec import SCHEMA, SDK_VERSION, encode_trace

_ACTIVE: ContextVar[TraceCapture | None] = ContextVar("local_trace_capture", default=None)
_LOG = logging.getLogger(__name__)
REF_KEY = "$trace_artifact"
LITERAL_KEY = "$trace_literal"


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def trace_id(source: dict[str, Any]) -> str:
    return "trace-" + hashlib.sha256(json_text(source).encode()).hexdigest()[:32]


class TraceCapture:
    def __init__(self, source: dict[str, Any], *, roots: dict[str, Any] | None = None,
                 secrets: tuple[str, ...] = ()) -> None:
        self.source = source
        self.ref = trace_id(source)
        self.started = time.time()
        self.events: list[dict[str, Any]] = []
        self.artifacts: dict[str, bytes] = {}
        self.partial: set[str] = set()
        self.roots = {**default_observability_roots(None), **(roots or {})}
        self.secrets = secrets
        self.lock = threading.RLock()

    def safe(self, value: Any) -> Any:
        result = redact_observability_payload(value, roots=self.roots, secrets=self.secrets)
        if result.truncated:
            self.partial.update(result.truncation_reasons or ("capture_failed",))
        return result.value

    def pack(self, value: Any) -> Any:
        # Redact logical leaves separately: a long conversation must not exhaust
        # a single traversal budget before its later messages are even visited.
        packed: Any
        if isinstance(value, dict):
            packed = {}
            for key, item in value.items():
                key = str(key)
                if isinstance(item, (dict, list, tuple)):
                    # Apply key-based secret filtering before descending.
                    marker = self.safe({key: "trace-value"})
                    packed[key] = marker[key] if marker[key] != "trace-value" else self.pack(item)
                else:
                    packed[key] = self.pack_safe(self.safe({key: item})[key])
            if REF_KEY in packed or LITERAL_KEY in packed:
                packed = {LITERAL_KEY: packed}
        elif isinstance(value, (list, tuple)):
            packed = [self.pack(item) for item in value]
        else:
            packed = self.safe(value)
        return self.pack_safe(packed)

    def pack_safe(self, value: Any) -> Any:
        raw = json_text(value).encode()
        if len(raw) < 2048:
            return value
        digest = hashlib.sha256(raw).hexdigest()
        self.artifacts.setdefault(digest, raw)
        return {REF_KEY: digest, "bytes": len(raw)}

    def record(self, event: dict[str, Any], body: Any = None) -> None:
        with self.lock:
            try:
                data = event.get("data") or {}
                if event.get("body_state") in {"truncated", "capture_failed"}:
                    self.partial.add(str(event["body_state"]))
                self.events.append({
                    "seq": len(self.events), "kind": str(event.get("kind", "event")),
                    "phase": str(event.get("phase") or ""), "status": str(event.get("status") or "recorded"),
                    "time": event.get("created_at") or time.time(), "data": self.safe(data),
                    "body": self.pack(body),
                })
            except Exception:
                self.partial.add("event_capture_failed")

    def agent_event(self, event: Any) -> None:
        try:
            projection = AgentProcessAdapter().project(event)
            if projection:
                self.record(projection.event, projection.body)
            else:
                from chatcopilot.contracts.agent import TopicDecisionMade
                from dataclasses import asdict
                if isinstance(event, TopicDecisionMade):
                    self.record({"kind": "context_selection", "status": "recorded",
                                 "data": {"context_kind": event.context_kind}}, asdict(event))
        except Exception:
            self.partial.add("agent_event_capture_failed")
            _LOG.warning("Local trace event capture failed")

    def finish(self, status: str, *, retained: bool = False, validate: bool = True) -> dict[str, Any]:
        with self.lock:
            finished = time.time()
            groups: dict[str, list[dict[str, Any]]] = {name: [] for name in
                ("baseSpans", "agentSpans", "llmSpans", "toolSpans", "retrieverSpans")}
            pairs: dict[tuple[str, str], dict[str, Any]] = {}
            identities: dict[tuple[str, str], str] = {}
            for event in self.events:
                data = event["data"]
                key = (str(data.get("trace_id") or ""), str(data.get("span_id") or ""))
                paired = bool(key[1] and event["phase"] in {"start", "finish"})
                span = pairs.get(key) if paired else None
                if span is None:
                    ident = hashlib.sha256(f"{self.ref}:{event['seq']}".encode()).hexdigest()
                    process = data.get("process_kind", "")
                    kind = "llm" if event["kind"].startswith("LlmCall") else "tool" if process == "tool" else (
                        "agent" if process in {"subagent", "workflow"} or data.get("runtime_layer") == "agent" else "base")
                    span = {"uuid": ident, "name": str(data.get("name") or data.get("operation") or event["kind"]),
                            "type": kind, "status": "SUCCESS", "startTime": _iso(event["time"]),
                            "endTime": _iso(event["time"]), "metadata": {"agentstrata": {"events": []}}}
                    groups[{"llm": "llmSpans", "tool": "toolSpans", "agent": "agentSpans", "base": "baseSpans"}[kind]].append(span)
                    if paired:
                        pairs[key] = span
                        identities[key] = ident
                meta = span["metadata"]["agentstrata"]
                meta["events"].append(event)
                meta.update(status=event["status"], order=meta.get("order", event["seq"]))
                if event["phase"] == "finish":
                    span["endTime"] = _iso(event["time"])
                    span["output"] = event["body"]
                elif event["phase"] == "start":
                    span["input"] = event["body"]
                else:
                    span["output"] = event["body"]
                if event["status"] in {"failed", "error", "cancelled", "aborted", "interrupted", "unknown"}:
                    span["status"] = "ERRORED"
                if data.get("model"):
                    span["model"] = str(data["model"])
                usage = data.get("usage") or {}
                for target, choices in (("inputTokenCount", ("input_tokens", "prompt_tokens")),
                                        ("outputTokenCount", ("output_tokens", "completion_tokens"))):
                    value = next((usage[k] for k in choices if k in usage), None)
                    if type(value) is int and value >= 0:
                        span[target] = value
            for spans in groups.values():
                for span in spans:
                    meta = span["metadata"]["agentstrata"]
                    first = meta["events"][0]
                    data = first["data"]
                    parent = data.get("parent_span_id") or data.get("stage_span_id")
                    if first["kind"] == "ContextSnapshotPrepared":
                        parent = data.get("span_id") or parent
                    resolved = identities.get((str(data.get("trace_id") or ""), str(parent or "")))
                    if resolved and resolved != span["uuid"]:
                        span["parentUuid"] = resolved
                    elif parent:
                        meta["external_parent"] = str(parent)
                    if first["phase"] == "start" and not any(e["phase"] == "finish" for e in meta["events"]):
                        meta["status"] = "incomplete"
                        span["status"] = "ERRORED"
                        meta["end_observed"] = False
                        self.partial.add("missing_end_event")
            meta = {"schema": SCHEMA, "sdk_version": SDK_VERSION, "source": self.safe(self.source),
                    "execution_status": status, "capture_state": "partial" if self.partial else "available" if self.events else "not_recorded",
                    "capture_reasons": sorted(self.partial), "started_at": self.started, "finished_at": finished,
                    "expires_at": None if retained else finished + 30 * 86400,
                    "artifacts": {key: len(raw) for key, raw in self.artifacts.items()}}
            result = {"uuid": self.ref, "name": str(self.source.get("kind", "execution")),
                "status": "SUCCESS" if status in {"completed", "passed", "succeeded", "fixed"} else "ERRORED",
                "startTime": _iso(self.started), "endTime": _iso(finished),
                "metadata": {"agentstrata": meta}, **groups}
            return encode_trace(result) if validate else result


@contextmanager
def capture_scope(capture: TraceCapture) -> Iterator[TraceCapture]:
    token = _ACTIVE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE.reset(token)


def current_capture() -> TraceCapture | None:
    return _ACTIVE.get()


def capture_agent_event(event: Any) -> None:
    capture = _ACTIVE.get()
    if capture is not None:
        capture.agent_event(event)
