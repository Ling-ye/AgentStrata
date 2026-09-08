"""Record actual runtime boundaries without taking ownership of their outcomes."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import time
from typing import Any, Iterator
from uuid import uuid4

from chatcopilot.core.observation_context import observe


AGENT_EXECUTION_SPAN_ID = "host:actor"
_STAGE: ContextVar[RuntimeStage | None] = ContextVar("runtime_observation_stage", default=None)


@dataclass
class RuntimeStage:
    operation: str
    runtime_layer: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    status: str = "succeeded"

    def complete(self, output: Any, *, status: str = "succeeded", **summary: Any) -> None:
        self.output = output
        self.status = status
        self.metadata.update(summary)


def current_runtime_stage() -> RuntimeStage | None:
    return _STAGE.get()


def capture_payload(factory: Any) -> Any:
    try:
        return factory()
    except Exception:
        return {"capture_state": "capture_failed"}


def _payload_projection(function):
    @wraps(function)
    def project(*args, **kwargs):
        return capture_payload(lambda: function(*args, **kwargs))
    return project


@contextmanager
def runtime_stage(operation: str, runtime_layer: str, *, trace_id: str,
                  input: Any = None, span_id: str | None = None,
                  parent_span_id: str | None = None, source: str | None = None,
                  target: str | None = None, occurred_at: float | None = None,
                  **metadata: Any) -> Iterator[RuntimeStage]:
    stage = RuntimeStage(operation, runtime_layer, trace_id, span_id or f"stage:{uuid4().hex}",
                         parent_span_id, {"source": source, "target": target, **metadata})
    token = _STAGE.set(stage)
    base = {"flow_version": 1, "runtime_layer": runtime_layer, "operation": operation,
            "trace_id": trace_id, "span_id": stage.span_id, "parent_span_id": parent_span_id}
    observe("runtime_stage", **base, **stage.metadata, phase="start", status="running",
            observed_at=occurred_at if occurred_at is not None else time.time(),
            captured_at=time.time(), body={"input": input})
    error = None
    try:
        yield stage
    except BaseException as exc:
        if stage.status == "succeeded":
            stage.status = "aborted" if isinstance(exc, asyncio.CancelledError) or type(exc).__name__ == "CancellationRequested" else "failed"
        error = capture_payload(lambda error=exc: {"code": str(getattr(error, "code", type(error).__name__)), "message": str(error)[:2048]})
        stage.metadata["code"] = error.get("code", type(exc).__name__)
        raise
    finally:
        body = {"output": stage.output}
        if error is not None:
            body["error"] = error
        observe("runtime_stage", **base, **stage.metadata, phase="finish", status=stage.status,
                observed_at=occurred_at if occurred_at is not None else time.time(),
                captured_at=time.time(), body=body)
        _STAGE.reset(token)


@_payload_projection
def resource_summary(resource: Any) -> dict[str, Any]:
    return {key: getattr(resource, key, None) for key in
            ("name", "kind", "media_type", "size_bytes", "sha256")}


@_payload_projection
def inbound_summary(event: Any) -> dict[str, Any]:
    evidence = event.evidence
    return {"event_id": evidence.event_id, "message_id": evidence.message_id,
            "channel": evidence.account.channel, "conversation_kind": evidence.conversation.kind,
            "frame_sha256": evidence.frame_sha256, "connection_generation": evidence.connection_generation,
            "segments": [{"kind": item.kind, "text": item.text, "target": item.target,
                          "resource_ticket_id": item.resource_ticket_id} for item in event.segments],
            "resources": [{"ticket_id": item.ticket_id, **resource_summary(item)} for item in event.resource_tickets]}


@_payload_projection
def channel_input_summary(event: Any) -> dict[str, Any]:
    observation = event.input_observation
    if observation is None:
        return {"capture_state": "not_recorded"}
    if observation.frame_sha256 != event.evidence.frame_sha256:
        return {"capture_state": "capture_failed", "omitted": ["input_event_binding_mismatch"]}
    return observation.to_payload()


@_payload_projection
def turn_summary(request: Any) -> dict[str, Any]:
    return {"text": request.canonical_text, "run_id": request.run_id, "session_id": request.session_id,
            "role": request.principal.role.value, "conversation_kind": request.principal.conversation.chat_kind,
            "resources": [resource_summary(item) for item in request.resource_refs],
            "turn_context": request.turn_context}


@_payload_projection
def result_summary(result: Any) -> dict[str, Any]:
    return {"final_text": result.final_text, "stop_reason": result.stop_reason,
            "message_count": result.message_count,
            "produced_resources": [resource_summary(item) for item in result.produced_resources]}


@_payload_projection
def outbound_summary(envelope: Any) -> dict[str, Any]:
    return {"outbound_id": envelope.outbound_id, "run_id": envelope.run_id,
            "session_id": envelope.session_id, "channel": envelope.account.channel,
            "conversation_kind": envelope.conversation.kind, "reply_to_message_id": envelope.reply_to_message_id,
            "segments": [{"kind": item.kind, "text": item.text, "target": item.target} for item in envelope.segments]}


@_payload_projection
def receipt_summary(receipt: Any) -> dict[str, Any]:
    return {key: getattr(receipt, key, None) for key in
            ("receipt_id", "outbound_id", "stage", "observed_at", "provider_message_id", "error_code")}
