"""Bounded diagnostics; authority remains with the run, policy and outbox owners."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
import logging
import time
from typing import Any

from chatcopilot.contracts.agent import (
    AgentEvent, ContextSnapshotPrepared, InputResourcesDispatched,
    LlmCallFinished, LlmCallStarted, SpanFinished, SpanStarted,
    ToolFinished, ToolStarted, ToolAuthorizationChecked, TurnError,
)
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets, default_observability_roots, redact_observability_payload,
    omit_private_reasoning_messages, omit_local_resource_paths,
)
from .state_store import GatewayStateStore

_LOG = logging.getLogger(__name__)


class RunObserver:
    def __init__(self, store: GatewayStateStore, generation: int, run_id: str) -> None:
        self.store = store
        self.generation = generation
        self.run_id = run_id
        self.recorder = getattr(store, "observation_recorder", None)
        self._full = False
        self._secrets = collect_observability_secrets()
        self._roots = default_observability_roots(store.root)

    def prepare(self, request: Any) -> None:
        if self.recorder is not None:
            try:
                self.recorder.prepare(self.run_id, request)
            except Exception:
                _LOG.warning("Gateway configuration observation unavailable")

    def scope(self):
        return self.recorder.scope(self.run_id) if self.recorder is not None else nullcontext()

    def record(self, kind: str, source: str, target: str, status: str, **data: Any) -> None:
        if self.recorder is not None:
            layer = {"model": "agent", "capability": "capability"}.get(target, target)
            entity = {"gateway": "gateway:instance", "application": "workspace:instance", "agent": "agent:main",
                      "channel": "channel:qq", "authorization": "policy:instance"}.get(layer)
            if kind in {"actor_execution", "actor_returned"}:
                data.update(trace_id=self.run_id, span_id="host:actor")
                layer, entity = "application", "workspace:instance"
            phase = "start" if kind == "actor_execution" else "finish" if kind == "actor_returned" else ""
            self.recorder.record(self.run_id, {"kind": kind, "layer": layer, "entity_id": entity,
                "source": source, "target": target, "status": status, "phase": phase, "data": data})
            return
        if self._full:
            return
        safe = redact_observability_payload(
            {"kind": kind, "source": source, "target": target, "status": status, "data": data},
            secrets=self._secrets, roots=self._roots,
        ).value
        safe["data"] = {key: value[:160] if isinstance(value, str) else value
                        for key, value in safe.get("data", {}).items()}
        try:
            self._full = not self.store.append_run_observation(
                generation=self.generation, run_id=self.run_id, payload=safe,
            )
        except Exception:
            # Diagnostic failure must not change an authorization or delivery outcome.
            self._full = True
            _LOG.warning("Gateway run observation unavailable")

    def __call__(self, event: AgentEvent) -> None:
        if self.recorder is not None:
            self._record_event(event)
            return
        data: dict[str, Any] = {}
        for name in ("model", "backend", "name", "span_id", "parent_span_id", "coverage", "code"):
            value = getattr(event, name, None)
            if isinstance(value, str):
                data[name] = value
        for name in ("iteration", "depth", "input_message_count", "input_estimated_tokens", "tool_schema_count"):
            value = getattr(event, name, None)
            if type(value) is int and 0 <= value <= 10**12:
                data[name] = value
        if isinstance(event, (LlmCallStarted, LlmCallFinished)):
            finished = isinstance(event, LlmCallFinished)
            if finished and event.usage:
                data["usage"] = {
                    key: value for key, value in event.usage.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
                    and type(value) is int and 0 <= value <= 10**12
                }
            self.record(type(event).__name__, "model" if finished else "agent",
                        "agent" if finished else "model",
                        ("succeeded" if event.ok else "failed") if finished else "running", **data)
        elif isinstance(event, (ToolStarted, ToolFinished, SpanStarted, SpanFinished)):
            finished = isinstance(event, (ToolFinished, SpanFinished))
            self.record(type(event).__name__, "capability" if finished else "agent",
                        "agent" if finished else "capability",
                        ("succeeded" if event.ok else "failed") if finished else "running", **data)
        elif isinstance(event, ContextSnapshotPrepared):
            data.update(message_count=len(event.effective_messages), tool_schema_count=len(event.tool_schemas),
                        resource_count=len(event.resources))
            self.record("context_prepared", "application", "agent", "succeeded", **data)
        elif isinstance(event, InputResourcesDispatched):
            self.record("resources_dispatched", "agent", "model", "succeeded", resource_count=len(event.resources))
        elif isinstance(event, TurnError):
            self.record("turn_error", "agent", "gateway", "failed", **data)

    def _record_event(self, event: AgentEvent) -> None:
        if isinstance(event, ToolAuthorizationChecked):
            self.recorder.host_event(self.run_id, "tool_authorization", asdict(event))
            return
        if not isinstance(event, (LlmCallStarted, LlmCallFinished, ToolStarted, ToolFinished,
                                  SpanStarted, SpanFinished, ContextSnapshotPrepared, InputResourcesDispatched, TurnError)):
            return
        data = {key: getattr(event, key) for key in (
            "name", "model", "backend", "trace_id", "span_id", "parent_span_id", "depth", "iteration",
            "coverage", "omitted", "code", "input_message_count", "input_estimated_tokens", "context_snapshot_id",
            "context_kind", "finish_reason", "estimated_tokens", "model_selection",
        ) if hasattr(event, key)}
        finished = isinstance(event, (LlmCallFinished, ToolFinished, SpanFinished))
        started = isinstance(event, (LlmCallStarted, ToolStarted, SpanStarted))
        status = ("succeeded" if event.ok else "failed") if finished else "running" if started else "succeeded"
        body: Any = None
        layer, entity = "agent", "agent:main"
        context = isinstance(event, ContextSnapshotPrepared)
        if isinstance(event, (ToolStarted, ToolFinished)):
            layer, entity = "capability", f"tool:{event.name}"
            body = asdict(event)
            if isinstance(event, ToolFinished) and isinstance(event.data, dict) and event.data.get("error_code"):
                data["code"] = str(event.data["error_code"])
            if isinstance(event, ToolFinished) and event.name in {"start_code_task", "get_code_task", "cancel_code_task", "resume_code_task"}:
                result = (event.data or {}).get("data") or {}
                if isinstance(result, dict) and isinstance(result.get("task_id"), str):
                    data.update(related_task_id=result["task_id"], related_task_state=result.get("status"),
                                relation="background_task", relation_capture="summary_only")
        elif isinstance(event, (LlmCallStarted, LlmCallFinished)):
            entity = f"model:{event.model}"
            if isinstance(event, LlmCallFinished) and event.usage:
                usage = dict(event.usage)
                details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
                if isinstance(details, dict) and type(details.get("cached_tokens")) is int:
                    usage["cached_tokens"] = details["cached_tokens"]
                if "total_tokens" not in usage:
                    incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
                    outgoing = usage.get("output_tokens", usage.get("completion_tokens"))
                    if type(incoming) is int and type(outgoing) is int:
                        usage["total_tokens"] = incoming + outgoing
                data["usage"] = {key: value for key, value in usage.items() if type(value) is int and value >= 0}
        elif context:
            layer, entity = "application", "workspace:instance"
            body = asdict(event)
            for key in ("session_messages", "effective_messages"):
                messages = omit_private_reasoning_messages(body[key]).messages
                body[key] = omit_local_resource_paths(messages).messages
            data["snapshot_id"] = event.snapshot_id
        elif isinstance(event, TurnError):
            status = "failed"
            body = {"code": event.code, "message": event.message}
        elif isinstance(event, (SpanStarted, SpanFinished)):
            if event.kind in {"subagent", "workflow"} and event.name.startswith(event.kind + ":"):
                entity = event.name
            body = asdict(event) if finished else None
        observed_at = getattr(event, "finished_at", None) or getattr(event, "started_at", None) or time.time()
        self.recorder.record(self.run_id, {"kind": type(event).__name__, "layer": layer, "entity_id": entity,
            "phase": "finish" if finished else "start" if started else "", "status": status,
            "created_at": observed_at, "data": data}, body=body, context=context)
