"""Backend-neutral AgentEvent projection for process observers.

Backend adapters own native formats. This pure projection owns the shared
observation shape; storage owners only add their task and stage bindings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any

from chatcopilot.contracts.agent import (
    AgentEvent,
    AgentContentDelta,
    AgentMessageObserved,
    ContextSnapshotPrepared,
    InputResourcesDispatched,
    LlmCallFinished,
    LlmCallStarted,
    SpanFinished,
    SpanStarted,
    SpanUpdated,
    ToolAuthorizationChecked,
    ToolFinished,
    ToolStarted,
    TurnError,
    ToolCatalogObserved,
)
from chatcopilot.core.observability_redaction import omit_private_reasoning_messages


@dataclass(frozen=True)
class ProcessObservation:
    event: dict[str, Any]
    body: Any = None
    context: bool = False


class AgentProcessAdapter:
    def project(self, event: AgentEvent) -> ProcessObservation | None:
        supported = (
            AgentContentDelta,
            ToolCatalogObserved,
            AgentMessageObserved,
            LlmCallStarted,
            LlmCallFinished,
            ToolStarted,
            ToolFinished,
            SpanStarted,
            SpanFinished,
            SpanUpdated,
            ContextSnapshotPrepared,
            InputResourcesDispatched,
            ToolAuthorizationChecked,
            TurnError,
        )
        if not isinstance(event, supported):
            return None
        data = {key: getattr(event, key) for key in (
            "name", "model", "backend", "trace_id", "span_id", "parent_span_id", "depth", "iteration",
            "coverage", "omitted", "code", "input_message_count", "input_estimated_tokens", "context_snapshot_id",
            "context_kind", "finish_reason", "estimated_tokens", "model_selection", "tool_schema_count",
            "system_estimated_tokens", "tool_schema_estimated_tokens", "estimator_version", "execution_kind",
            "source", "tool_call_id", "model_span_id", "revision", "message_id", "message_kind",
            "item_id", "content_kind", "section",
        ) if hasattr(event, key)}
        finished = isinstance(event, (LlmCallFinished, ToolFinished, SpanFinished))
        started = isinstance(event, (LlmCallStarted, ToolStarted, SpanStarted))
        phase = "finish" if finished else "start" if started else ""
        status = ("succeeded" if event.ok else "failed") if finished else "running" if started else "succeeded"
        body: Any = None
        body_state = None
        layer, entity = "agent", "agent:main"
        context = isinstance(event, ContextSnapshotPrepared)
        process_kind = "context" if context else "event"
        kind = type(event).__name__
        if isinstance(event, AgentContentDelta):
            process_kind = {"message": "message", "public_summary": "reasoning", "command_output": "command"}[event.content_kind]
            phase, status = "update", "running"
            body = {"delta": event.text, "section": event.section, "content_kind": event.content_kind}
            body_state = event.capture_state
        elif isinstance(event, ToolCatalogObserved):
            process_kind = "tool_catalog"
            data.update(
                catalog_phase=event.phase,
                catalog_tool_count=len(event.tools),
                code=event.error_code,
            )
            status = "failed" if event.error_code else "succeeded"
            body = {
                "tools": event.tools,
                "source": event.source,
                "catalog_phase": event.phase,
                "error_code": event.error_code,
            }
        elif isinstance(event, (ToolStarted, ToolFinished)):
            process_kind = "tool"
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
            process_kind = event.execution_kind
            entity = f"model:{event.model}"
            data["source"] = "adapter" if event.execution_kind == "backend_execution" else "host"
            if isinstance(event, LlmCallStarted) and event.request_parameters is not None:
                body = {"request_parameters": event.request_parameters}
            if isinstance(event, LlmCallFinished) and event.visible_response is not None:
                body = {"visible_response": event.visible_response}
                body_state = event.visible_response.get("capture_state")
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
        elif isinstance(event, AgentMessageObserved):
            process_kind, phase, status = "message", event.phase, event.status
            body = {"text": event.text, "message_kind": event.message_kind}
            body_state = event.capture_state
        elif isinstance(event, (SpanStarted, SpanFinished, SpanUpdated)):
            process_kind = event.kind
            if event.kind in {"subagent", "workflow"} and event.name.startswith(event.kind + ":"):
                entity = event.name
            body = dict(event.data) if event.data is not None else None
            if isinstance(event, SpanFinished):
                body = {**(body or {}), "summary": event.summary}
            if isinstance(event, SpanUpdated):
                phase, status = "update", "running"
            if body:
                body_state = body.get("capture_state")
                # Only structural capture metadata survives the detail retention window.
                if body.get("status") in {"incomplete", "cancelled", "unknown", "truncated"}:
                    status = str(body["status"])
        elif context:
            layer, entity = "application", "workspace:instance"
            body = asdict(event)
            for key in ("session_messages", "effective_messages"):
                omitted = omit_private_reasoning_messages(body[key])
                body[key] = omitted.messages
                body["private_reasoning_omission_count"] += omitted.omission_count
            data["snapshot_id"] = event.snapshot_id
        elif isinstance(event, ToolAuthorizationChecked):
            process_kind, kind, layer = "permission", "tool_authorization", "authorization"
            entity, status = f"tool:{event.name}", "succeeded" if event.allowed else "failed"
            body = asdict(event)
            data.update(body)
        elif isinstance(event, TurnError):
            process_kind, status = "error", "failed"
            body = {"code": event.code, "message": event.message}
        elif isinstance(event, InputResourcesDispatched):
            process_kind = "resources"
            data.update(request_id=event.request_id, resource_count=len(event.resources))
            body = {"backend": event.backend, "request_id": event.request_id,
                    "turn_index": event.turn_index, "resources": [asdict(item) for item in event.resources]}
        data["process_kind"] = process_kind
        observed_at = (getattr(event, "observed_at", None) or getattr(event, "finished_at", None)
                       or getattr(event, "started_at", None) or time.time())
        return ProcessObservation({"kind": kind, "layer": layer, "entity_id": entity,
            "phase": phase, "status": status, "created_at": observed_at,
            "data": data, "body_state": body_state}, body, context)
