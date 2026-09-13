"""Bounded diagnostics; authority remains with the run, policy and outbox owners."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
from typing import Any

from chatcopilot.contracts.agent import (
    AgentEvent, ContextSnapshotPrepared, InputResourcesDispatched,
    LlmCallFinished, LlmCallStarted, SpanFinished, SpanStarted,
    ToolFinished, ToolStarted, TurnError,
)
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets, default_observability_roots, redact_observability_payload,
)
from chatcopilot.core.runtime_observation import AGENT_EXECUTION_SPAN_ID, current_runtime_stage
from chatcopilot.core.observation_context import observation_scope
from chatcopilot.core.agent_process import AgentProcessAdapter
from .state_store import GatewayStateStore

_LOG = logging.getLogger(__name__)
ACTOR_SPAN_ID = AGENT_EXECUTION_SPAN_ID


def response_outbound_id(run_id: str) -> str:
    return "outbound_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


class RunObserver:
    def __init__(self, store: GatewayStateStore, generation: int, run_id: str,
                 *, agent_stage_span_id: str | None = None) -> None:
        self.store = store
        self.generation = generation
        self.run_id = run_id
        self.recorder = getattr(store, "observation_recorder", None)
        self._full = False
        self._secrets = collect_observability_secrets()
        self._roots = default_observability_roots(store.root)
        self.agent_stage_span_id = agent_stage_span_id
        self.process_adapter = AgentProcessAdapter()

    def accepted(self, text: str, role: str) -> None:
        if self.recorder is not None:
            try:
                self.recorder.accepted(self.run_id, text, role, secrets=tuple(self._secrets), roots=self._roots)
            except Exception:
                _LOG.warning("Gateway accepted input observation unavailable")

    def prepare(self, request: Any) -> None:
        if self.recorder is not None:
            try:
                self.recorder.prepare(self.run_id, request)
            except Exception:
                _LOG.warning("Gateway configuration observation unavailable")

    @contextmanager
    def scope(self):
        try:
            context = self.recorder.scope(self.run_id) if self.recorder is not None else observation_scope(self._fallback_stage)
            context.__enter__()
        except Exception:
            _LOG.warning("Gateway observation scope unavailable")
            yield
            return
        try:
            yield
        finally:
            try:
                context.__exit__(None, None, None)
            except Exception:
                _LOG.warning("Gateway observation scope cleanup unavailable")

    def _fallback_stage(self, kind: str, data: dict[str, Any]) -> None:
        if kind != "runtime_stage" or self._full:
            return
        metadata = {key: value for key, value in data.items()
                    if key not in {"body", "phase", "status", "observed_at"}}
        safe = redact_observability_payload({
            "kind": "RuntimeStageStarted" if data["phase"] == "start" else "RuntimeStageFinished",
            "layer": data["runtime_layer"], "phase": data["phase"], "status": data["status"],
            "trace_id": data["trace_id"], "span_id": data["span_id"], "parent_span_id": data.get("parent_span_id"),
            "created_at": data["observed_at"], "body_state": "not_recorded", "data": metadata,
        }, secrets=self._secrets, roots=self._roots).value
        try:
            self._full = not self.store.append_run_observation(
                generation=self.generation, run_id=self.run_id, payload=safe)
        except Exception:
            self._full = True
            _LOG.warning("Gateway run observation unavailable")

    def record(self, kind: str, source: str, target: str, status: str, **data: Any) -> None:
        if self.recorder is not None:
            layer = {"model": "agent", "capability": "capability"}.get(target, target)
            entity = {"gateway": "gateway:instance", "application": "workspace:instance", "agent": "agent:main",
                      "channel": "channel:qq", "authorization": "policy:instance"}.get(layer)
            self.recorder.record(self.run_id,
                {"kind": kind, "layer": layer, "entity_id": entity,
                 "source": source, "target": target, "status": status, "data": data})
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
            try:
                self._record_event(event)
            except Exception:
                _LOG.warning("Gateway Agent observation unavailable")
                try:
                    self.recorder.record(self.run_id, {
                        "kind": "AgentProcessCaptureFailed", "layer": "agent", "status": "unknown",
                        "body_state": "capture_failed", "data": {"flow_version": 1, "runtime_layer": "agent",
                            "trace_id": self.run_id, "stage_span_id": self.agent_stage_span_id,
                            "code": "agent_event_projection_failed"}})
                except Exception:
                    _LOG.warning("Gateway Agent observation failure could not be recorded")
            return
        data: dict[str, Any] = {}
        for name in ("model", "backend", "name", "trace_id", "span_id", "parent_span_id", "coverage", "code"):
            value = getattr(event, name, None)
            if isinstance(value, str):
                data[name] = value
        if self.agent_stage_span_id:
            data.update(flow_version=1, runtime_layer="agent", stage_span_id=self.agent_stage_span_id)
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
        projection = self.process_adapter.project(event)
        if projection is None:
            return
        record = projection.event
        data = record["data"]
        stage = current_runtime_stage()
        stage_id = stage.span_id if stage is not None and stage.runtime_layer == "agent" else self.agent_stage_span_id
        if stage_id:
            data.update(flow_version=1, runtime_layer="agent", stage_span_id=stage_id)
            if stage is not None and stage.runtime_layer == "agent" and not data.get("trace_id"):
                data["trace_id"] = stage.trace_id
        self.recorder.record(self.run_id, record, body=projection.body, context=projection.context)
