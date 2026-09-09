"""Project Codex CLI JSONL onto the shared Agent event protocol.

The projector records portable lifecycles and adapter-visible response text.
Only explicitly selected public item fields enter private observations;
provider-private reasoning and raw protocol frames are excluded.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from chatcopilot.contracts.agent import (
    AgentMessageObserved,
    EventSink,
    LlmCallFinished,
    SpanFinished,
    SpanStarted,
    SpanUpdated,
)
from chatcopilot.core.observability_redaction import bound_observability_payload
from chatcopilot.core.visible_model_response import project_visible_response
from chatcopilot.external_tools.codex_cli.process_runner import (
    STREAM_LINE_OMISSION_NOTICE,
)


_ITEM_KINDS = {
    "command_execution": "command",
    "command": "command",
    "file_change": "file_change",
    "file_changes": "file_change",
    "mcp_tool_call": "mcp_tool",
    "mcp_call": "mcp_tool",
    "web_search": "web_search",
    "plan_update": "plan",
    "todo_list": "plan",
    "reasoning": "reasoning",
}

_MAX_USAGE_TOKEN_COUNT = (1 << 63) - 1
_MAX_PROJECTED_PROVIDER_ITEMS = 500
_MAX_TRACKED_OMITTED_STARTED_ITEMS = 1024
_MAX_FINAL_TEXT_CHARS = 1024 * 1024
_MAX_PROVIDER_METADATA_CHARS = 512


@dataclass(frozen=True)
class _ProjectedSpan:
    name: str
    kind: str
    span_id: str


@dataclass
class CodexJsonlProjector:
    """Stateful, order-preserving projection for one ``codex exec --json`` turn."""

    model: str
    iteration: int
    trace_id: str
    llm_span_id: str
    parent_span_id: str | None
    context_snapshot_id: str
    on_event: EventSink
    on_thread_started: Callable[[str], None]
    depth: int = 0
    input_message_count: int = 0
    input_estimated_tokens: int = 0
    system_estimated_tokens: int = 0
    tool_schema_count: int = 0
    tool_schema_estimated_tokens: int = 0
    estimator_version: str = ""
    context_kind: str = ""
    line_count: int = 0
    final_parts: list[str] = field(default_factory=list)
    _active_spans: dict[str, _ProjectedSpan] = field(default_factory=dict)
    _completed_items: set[str] = field(default_factory=set)
    _llm_finished: bool = False
    _pending_finish_reason: str = ""
    _pending_usage: Mapping[str, int] | None = None
    _pending_ok: bool | None = None
    provider_failed: bool = False
    stream_omission_count: int = 0
    provider_item_omission_count: int = 0
    final_text_truncated: bool = False
    _final_text_chars: int = 0
    _stream_limit_reported: bool = False
    _provider_item_limit_reported: bool = False
    _omitted_started_items: set[str] = field(default_factory=set)
    _untracked_omitted_started_items: int = 0
    _last_complete_final_line: int = 0
    _last_stream_omission_line: int = 0
    _message_completed: set[str] = field(default_factory=set)
    _active_messages: dict[str, AgentMessageObserved] = field(default_factory=dict)
    _revisions: dict[str, int] = field(default_factory=dict)
    _updated_at: dict[str, float] = field(default_factory=dict)

    def consume_line(self, raw_line: str) -> None:
        """Consume one JSONL line. Invalid/non-object lines are ignored."""

        self.line_count += 1
        if raw_line == STREAM_LINE_OMISSION_NOTICE:
            self.mark_stream_line_omitted(line_number=self.line_count)
            return
        try:
            payload = json.loads(raw_line)
        except (TypeError, ValueError, RecursionError):
            return
        if not isinstance(payload, dict):
            return

        event_type = _normalized_type(payload.get("type"))
        if event_type == "thread.started":
            native_id = _bounded_text(
                payload.get("thread_id")
                or payload.get("threadId")
                or payload.get("id")
                or "",
                _MAX_PROVIDER_METADATA_CHARS,
            ).strip()
            if native_id:
                self.on_thread_started(native_id)
            return

        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = payload.get("item")
            if not isinstance(item, dict):
                return
            item_type = _normalized_item_type(item.get("type"))
            identity = _item_identity(item, item_type) or f"unlinked_{self.line_count}"
            if item_type in {"agent_message", "message"}:
                self._message_item(identity, item, completed=event_type == "item.completed")
                return
            kind = _ITEM_KINDS.get(item_type)
            if kind is None:
                return
            if event_type == "item.started":
                self._start_item(identity, item, item_type, kind)
            elif event_type == "item.updated":
                self._update_item(identity, item, item_type, kind)
            else:
                self._finish_item(identity, item, item_type, kind)
            return

        if event_type == "turn.completed":
            self._close_active_spans(
                ok=False,
                summary="provider item completion not observed",
            )
            self._remember_terminal(
                finish_reason=str(payload.get("finish_reason") or "completed"),
                usage=_normalize_usage(payload.get("usage")),
                ok=True,
            )
            return

        if event_type in {"turn.failed", "turn.cancelled"}:
            self.provider_failed = True
            self._close_active_spans(ok=False, summary=event_type)
            self._remember_terminal(
                finish_reason="cancelled" if event_type.endswith("cancelled") else "failed",
                usage=_normalize_usage(payload.get("usage")),
                ok=False,
            )
            return

        if event_type == "error":
            self.provider_failed = True
            self._close_active_spans(ok=False, summary="provider error")
            self._remember_terminal(finish_reason="failed", usage=None, ok=False)

    def consume_text(self, stdout: str) -> None:
        for line in (stdout or "").splitlines():
            self.consume_line(line)

    @property
    def final_text(self) -> str:
        return "\n".join(self.final_parts).strip()

    def finish(self, *, returncode: int) -> None:
        """Close incomplete provider spans after the process has exited."""

        ok = (
            returncode == 0
            and not self.provider_failed
            and self._pending_ok is not False
        )
        self._close_active_spans(
            ok=False,
            summary="provider item completion not observed",
        )
        self._emit_stream_omission()
        self._emit_provider_item_omission()
        self._finish_llm(
            finish_reason=(
                self._pending_finish_reason
                if ok and self._pending_finish_reason
                else self._pending_finish_reason
                if not ok and self._pending_ok is False
                else "completed" if ok else "failed"
            ),
            usage=self._pending_usage,
            ok=ok,
        )

    def fail(self, *, reason: str = "backend_failed") -> None:
        self.provider_failed = True
        self._close_active_spans(ok=False, summary=reason)
        self._emit_stream_omission()
        self._emit_provider_item_omission()
        self._finish_llm(finish_reason="failed", usage=None, ok=False)

    def mark_stream_line_omitted(self, *, line_number: int | None = None) -> None:
        """Expose a bounded telemetry gap without failing an intact final reply."""

        self.stream_omission_count += 1
        observed_line = self.line_count + 1 if line_number is None else line_number
        self._last_stream_omission_line = max(
            self._last_stream_omission_line,
            observed_line,
        )

    def _emit_stream_omission(self) -> None:
        if not self.stream_omission_count or self._stream_limit_reported:
            return
        self._stream_limit_reported = True
        span_id = _item_span_id(self.trace_id, "stream-record-size-limit")
        name = "Provider activity omitted by record size limit"
        self.on_event(
            SpanStarted(
                name=name,
                kind="provider_omission",
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=self.llm_span_id,
                depth=self.depth + 1,
            )
        )
        self.on_event(
            SpanFinished(
                name=name,
                kind="provider_omission",
                ok=False,
                summary="Oversized provider JSONL records were not retained or projected.",
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=self.llm_span_id,
                depth=self.depth + 1,
                data={
                    "status": "truncated",
                    "reason": "stream_record_size_limit",
                    "omitted_count": self.stream_omission_count,
                },
            )
        )

    def _append_final_text(self, text: str) -> None:
        if self.final_text_truncated:
            return
        separator_chars = 1 if self.final_parts else 0
        remaining = _MAX_FINAL_TEXT_CHARS - self._final_text_chars
        required = separator_chars + len(text)
        if required <= remaining:
            self.final_parts.append(text)
            self._final_text_chars += required
            self._last_complete_final_line = self.line_count
            return
        self.final_text_truncated = True
        keep = max(0, remaining - separator_chars)
        if keep:
            self.final_parts.append(text[:keep])
            self._final_text_chars += separator_chars + keep

    @property
    def has_complete_final_after_stream_omission(self) -> bool:
        if not self.stream_omission_count:
            return bool(self.final_text)
        return self._last_complete_final_line > self._last_stream_omission_line

    def _mark_provider_item_omitted(
        self,
        identity: str,
        *,
        from_completion: bool,
    ) -> None:
        if from_completion:
            if identity in self._omitted_started_items:
                self._omitted_started_items.remove(identity)
                return
            if self._untracked_omitted_started_items:
                self._untracked_omitted_started_items -= 1
                return
            self.provider_item_omission_count += 1
            return
        if identity in self._omitted_started_items:
            return
        self.provider_item_omission_count += 1
        if len(self._omitted_started_items) < _MAX_TRACKED_OMITTED_STARTED_ITEMS:
            self._omitted_started_items.add(identity)
        else:
            self._untracked_omitted_started_items += 1

    def _emit_provider_item_omission(self) -> None:
        if not self.provider_item_omission_count or self._provider_item_limit_reported:
            return
        self._provider_item_limit_reported = True
        span_id = _item_span_id(self.trace_id, "provider-item-limit")
        name = "Provider activity omitted by turn limit"
        self.on_event(
            SpanStarted(
                name=name,
                kind="provider_omission",
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=self.llm_span_id,
                depth=self.depth + 1,
            )
        )
        self.on_event(
            SpanFinished(
                name=name,
                kind="provider_omission",
                ok=False,
                summary="Additional provider activity exceeded the per-turn projection limit.",
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=self.llm_span_id,
                depth=self.depth + 1,
                data={
                    "status": "truncated",
                    "projected_item_limit": _MAX_PROJECTED_PROVIDER_ITEMS,
                    "omitted_count": self.provider_item_omission_count,
                },
            )
        )

    def _start_item(
        self,
        identity: str,
        item: Mapping[str, Any],
        item_type: str,
        kind: str,
        *,
        from_completion: bool = False,
    ) -> _ProjectedSpan | None:
        existing = self._active_spans.get(identity)
        if existing is not None:
            return existing
        if identity in self._completed_items:
            return _ProjectedSpan(
                name=_item_name(item, item_type),
                kind=kind,
                span_id=_item_span_id(self.trace_id, identity),
            )
        if (
            len(self._active_spans) + len(self._completed_items)
            >= _MAX_PROJECTED_PROVIDER_ITEMS
        ):
            self._mark_provider_item_omitted(
                identity,
                from_completion=from_completion,
            )
            return None
        projected = _ProjectedSpan(
            name=_item_name(item, item_type),
            kind=kind,
            span_id=_item_span_id(self.trace_id, identity),
        )
        self._active_spans[identity] = projected
        if not from_completion:
            self.on_event(SpanStarted(name=projected.name, kind=projected.kind,
                trace_id=self.trace_id, span_id=projected.span_id, parent_span_id=self.llm_span_id,
                depth=self.depth + 1, backend="codex", source="provider", observed_at=time.time(),
                data=_item_payload(item, item_type, completed=False)))
        return projected

    def _finish_item(
        self,
        identity: str,
        item: Mapping[str, Any],
        item_type: str,
        kind: str,
    ) -> None:
        if identity in self._completed_items:
            return
        projected = self._start_item(
            identity,
            item,
            item_type,
            kind,
            from_completion=True,
        )
        if projected is None:
            return
        self._active_spans.pop(identity, None)
        self._completed_items.add(identity)
        ok = _item_succeeded(item)
        summary, data = _item_completion(item, item_type, ok=ok)
        self.on_event(
            SpanFinished(
                name=projected.name,
                kind=projected.kind,
                ok=ok,
                summary=summary,
                trace_id=self.trace_id,
                span_id=projected.span_id,
                parent_span_id=self.llm_span_id,
                depth=self.depth + 1,
                data=data, backend="codex", source="provider", observed_at=time.time(),
            )
        )

    def _update_allowed(self, identity: str) -> bool:
        revision = self._revisions.get(identity, 0)
        now = time.monotonic()
        if revision >= 32 or now - self._updated_at.get(identity, 0) < 1:
            return False
        self._updated_at[identity] = now
        return True

    def _revision(self, identity: str) -> int:
        revision = self._revisions.get(identity, 0) + 1
        self._revisions[identity] = revision
        return revision

    def _update_item(self, identity: str, item: Mapping[str, Any], item_type: str, kind: str) -> None:
        if identity in self._completed_items:
            return
        projected = self._active_spans.get(identity) or self._start_item(
            identity, item, item_type, kind, from_completion=True)
        if projected is None or not self._update_allowed(identity):
            return
        self.on_event(SpanUpdated(name=projected.name, kind=kind,
            trace_id=self.trace_id, span_id=projected.span_id, parent_span_id=self.llm_span_id,
            depth=self.depth + 1, backend="codex", source="provider", observed_at=time.time(),
            revision=self._revision(identity), data=_item_payload(item, item_type, completed=False)))

    def _message_item(self, identity: str, item: Mapping[str, Any], *, completed: bool) -> None:
        if identity in self._message_completed:
            return
        text = _message_text(item)
        # Delivery assembly precedes telemetry budgets: dropping an observation
        # must never drop a provider's completed reply.
        if completed:
            if len(self._message_completed) < _MAX_PROJECTED_PROVIDER_ITEMS:
                self._message_completed.add(identity)
            self._active_messages.pop(identity, None)
            if text:
                self._append_final_text(text)
        if identity not in self._revisions and len(self._revisions) >= _MAX_PROJECTED_PROVIDER_ITEMS:
            self._mark_provider_item_omitted(identity, from_completion=completed)
            return
        if not completed and not self._update_allowed(identity):
            return
        if not text:
            return
        raw = text.encode("utf-8", errors="replace")
        event = AgentMessageObserved(text=raw[:48 * 1024].decode("utf-8", errors="ignore"),
            message_id=identity, trace_id=self.trace_id, span_id=_item_span_id(self.trace_id, identity),
            parent_span_id=self.llm_span_id, revision=self._revision(identity),
            phase="finish" if completed else "update", status="succeeded" if completed else "running",
            message_kind="progress" if item.get("phase") == "commentary" else
                "final" if item.get("phase") == "final_answer" else "response",
            backend="codex", source="provider", depth=self.depth + 1, observed_at=time.time(),
            capture_state="truncated" if len(raw) > 48 * 1024 else "available")
        if completed:
            self._active_messages.pop(identity, None)
        else:
            self._active_messages[identity] = event
        self.on_event(event)

    def _close_active_spans(self, *, ok: bool, summary: str) -> None:
        for identity, event in self._active_messages.items():
            self.on_event(replace(event, phase="finish", revision=self._revision(identity), observed_at=time.time(),
                status="cancelled" if "cancel" in summary else "incomplete" if "completion not observed" in summary else "failed"))
            self._message_completed.add(identity)
        self._active_messages.clear()
        for identity, projected in tuple(self._active_spans.items()):
            self.on_event(
                SpanFinished(
                    name=projected.name,
                    kind=projected.kind,
                    ok=ok,
                    summary=summary,
                    trace_id=self.trace_id,
                    span_id=projected.span_id,
                    parent_span_id=self.llm_span_id,
                    depth=self.depth + 1,
                    backend="codex", source="provider", observed_at=time.time(),
                    data={
                        "status": (
                            "incomplete"
                            if summary == "provider item completion not observed"
                            else "completed" if ok else "failed"
                        )
                    },
                )
            )
            self._completed_items.add(identity)
        self._active_spans.clear()

    def _finish_llm(
        self,
        *,
        finish_reason: str,
        usage: Mapping[str, int] | None,
        ok: bool,
    ) -> None:
        if self._llm_finished:
            return
        self._llm_finished = True
        self.on_event(
            LlmCallFinished(
                model=self.model,
                iteration=self.iteration,
                backend="codex",
                execution_kind="backend_execution",
                finish_reason=finish_reason,
                usage=usage,
                trace_id=self.trace_id,
                span_id=self.llm_span_id,
                parent_span_id=self.parent_span_id,
                depth=self.depth,
                input_message_count=self.input_message_count,
                input_estimated_tokens=self.input_estimated_tokens,
                system_estimated_tokens=self.system_estimated_tokens,
                tool_schema_count=self.tool_schema_count,
                tool_schema_estimated_tokens=self.tool_schema_estimated_tokens,
                estimator_version=self.estimator_version,
                context_kind=self.context_kind,
                context_snapshot_id=self.context_snapshot_id,
                ok=ok,
                visible_response=project_visible_response(
                    self.final_text, coverage="adapter_visible",
                    omitted=("provider_internal_turns", "provider_private_reasoning"),
                    truncated=self.final_text_truncated or bool(self.stream_omission_count),
                ),
            )
        )

    def _remember_terminal(
        self,
        *,
        finish_reason: str,
        usage: Mapping[str, int] | None,
        ok: bool,
    ) -> None:
        if self._pending_ok is False and ok:
            return
        self._pending_finish_reason = _bounded_text(
            finish_reason,
            _MAX_PROVIDER_METADATA_CHARS,
        )
        if usage is not None:
            self._pending_usage = usage
        self._pending_ok = ok


def _normalized_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", ".")


def _normalized_item_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _message_text(item: Mapping[str, Any]) -> str:
    raw = item.get("text")
    if raw is None:
        raw = item.get("content")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = [
            str(part.get("text") or "").strip()
            for part in raw
            if isinstance(part, dict) and str(part.get("text") or "").strip()
        ]
        return "\n".join(parts).strip()
    return ""


def _item_identity(item: Mapping[str, Any], item_type: str) -> str:
    explicit = str(item.get("id") or item.get("call_id") or "").strip()
    if explicit:
        digest = hashlib.sha256(explicit.encode("utf-8", errors="replace")).hexdigest()
        return "provider_" + digest[:24]
    return ""


def _item_span_id(trace_id: str, identity: str) -> str:
    raw = f"{trace_id}\0codex-item\0{identity}".encode("utf-8", errors="replace")
    return "span_" + hashlib.sha256(raw).hexdigest()[:12]


def _item_name(item: Mapping[str, Any], item_type: str) -> str:
    if item_type in {"mcp_tool_call", "mcp_call"}:
        server = _bounded_text(
            item.get("server") or item.get("server_name") or "",
            96,
        ).strip()
        tool = _bounded_text(
            item.get("tool") or item.get("tool_name") or item.get("name") or "",
            128,
        ).strip()
        qualified = ".".join(part for part in (server, tool) if part)
        return f"Codex MCP {qualified}" if qualified else "Codex MCP tool"
    return {
        "command_execution": "Codex command",
        "command": "Codex command",
        "file_change": "Codex file change",
        "file_changes": "Codex file change",
        "web_search": "Codex web search",
        "plan_update": "Codex plan update",
        "todo_list": "Codex plan update",
        "reasoning": "Codex reasoning",
    }.get(item_type, "Codex operation")


def _item_succeeded(item: Mapping[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    if status in {"failed", "error", "cancelled", "canceled"}:
        return False
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return exit_code == 0
    error = item.get("error")
    if error is None or error is False:
        return True
    return isinstance(error, str) and not error.strip()


def _item_completion(
    item: Mapping[str, Any],
    item_type: str,
    *,
    ok: bool,
) -> tuple[str, dict[str, Any]]:
    status = _bounded_text(
        item.get("status") or ("completed" if ok else "failed"),
        64,
    ).strip().lower()
    data: dict[str, Any] = {
        "provider_item_type": item_type,
        "status": status,
    }
    exit_code = item.get("exit_code")
    if (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and -(1 << 63) < exit_code < (1 << 63)
    ):
        data["exit_code"] = exit_code
    if item_type in {"file_change", "file_changes"}:
        changes = item.get("changes")
        if isinstance(changes, list):
            data["change_count"] = len(changes)
    if item_type in {"plan_update", "todo_list"}:
        steps = item.get("plan") or item.get("items")
        if isinstance(steps, list):
            data["step_count"] = len(steps)
    data.update(_item_payload(item, item_type, completed=True))
    detail = f" ({data['exit_code']})" if "exit_code" in data else ""
    return f"{status}{detail}", data


def _item_payload(item: Mapping[str, Any], item_type: str, *, completed: bool) -> dict[str, Any]:
    fields = {
        "command_execution": ("command",), "command": ("command",),
        "mcp_tool_call": ("server", "tool", "arguments"), "mcp_call": ("server", "tool", "arguments"),
        "web_search": ("query",), "file_change": (), "file_changes": (),
        "plan_update": (), "todo_list": (), "reasoning": (),
    }.get(item_type, ())
    inputs = {key: item[key] for key in fields if key in item}
    outputs: dict[str, Any] = {}
    if item_type in {"command_execution", "command"}:
        keys = ("aggregated_output", "exit_code")
    elif item_type in {"mcp_tool_call", "mcp_call"}:
        keys = ("result",)
    elif item_type in {"file_change", "file_changes"}:
        keys = ("changes",)
    elif item_type in {"plan_update", "todo_list"}:
        keys = ("plan", "items")
    elif item_type == "web_search":
        keys = ("results",)
    else:
        keys = ()
    outputs.update({key: item[key] for key in keys if key in item})
    if item_type == "reasoning":
        # The public summary field is separate from opaque reasoning text.
        if isinstance(item.get("summary"), (str, list)):
            outputs["public_summary"] = item["summary"]
    payload: dict[str, Any] = {"input": inputs or None, "output": outputs or None}
    if completed and item.get("error"):
        error = item["error"]
        payload["error"] = ({key: error[key] for key in ("code", "message") if key in error}
                            if isinstance(error, dict) else error if isinstance(error, str) else "provider_error")
    bounded = bound_observability_payload(payload)
    return {**bounded.value, "capture_state": "truncated" if bounded.truncated else "available"}


def _bounded_text(value: Any, limit: int) -> str:
    try:
        text = str(value or "")
    except (ValueError, RecursionError):
        return "[invalid provider text]"
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if normalized < 0 or normalized > _MAX_USAGE_TOKEN_COUNT:
        return 0
    return normalized


def _normalize_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, Mapping):
        return None
    prompt = _nonnegative_int(raw.get("input_tokens") or raw.get("prompt_tokens"))
    completion = _nonnegative_int(raw.get("output_tokens") or raw.get("completion_tokens"))
    cached = _nonnegative_int(
        raw.get("cached_input_tokens")
        or raw.get("cached_tokens")
        or raw.get("cache_read_tokens")
    )
    reasoning = _nonnegative_int(
        raw.get("reasoning_output_tokens") or raw.get("reasoning_tokens")
    )
    cache_write = _nonnegative_int(
        raw.get("cache_write_input_tokens") or raw.get("cache_write_tokens")
    )
    derived_total = min(_MAX_USAGE_TOKEN_COUNT, prompt + completion)
    total = _nonnegative_int(raw.get("total_tokens")) or derived_total
    normalized = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "reasoning_tokens": reasoning,
        "cached_tokens": cached,
        "cache_read_tokens": cached,
        "cache_write_tokens": cache_write,
    }
    return normalized if any(normalized.values()) else None


__all__ = ["CodexJsonlProjector"]
