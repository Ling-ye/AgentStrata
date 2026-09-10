"""Project the public App Server item protocol into portable Agent events."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable

from chatcopilot.agent.backends.codex_events import CodexJsonlProjector, _item_span_id
from chatcopilot.contracts.agent import AgentContentDelta, AgentMessageObserved, SpanFinished, SpanStarted, SpanUpdated


_ITEMS = {
    "agentMessage": ("agent_message", "message"),
    "reasoning": ("reasoning", "reasoning"),
    "commandExecution": ("command_execution", "command"),
    "mcpToolCall": ("mcp_tool_call", "mcp_tool"),
    "fileChange": ("file_change", "file_change"),
    "webSearch": ("web_search", "web_search"),
    "plan": ("plan_update", "plan"),
    "collabAgentToolCall": ("subagent", "subagent"),
}
_DELTAS = {
    "item/agentMessage/delta": "message",
    "item/reasoning/summaryTextDelta": "public_summary",
    "item/commandExecution/outputDelta": "command_output",
}
_TOKEN_KEYS = {"inputTokens": "prompt_tokens", "outputTokens": "completion_tokens",
    "cachedInputTokens": "cached_tokens", "reasoningOutputTokens": "reasoning_tokens",
    "totalTokens": "total_tokens", "cacheWriteInputTokens": "cache_write_tokens"}
_CONTENT_BYTES = 48 * 1024


@dataclass
class AppServerProjector(CodexJsonlProjector):
    thread_id: str = ""
    turn_id: str = ""
    initial_usage: dict[str, int] | None = None
    on_usage: Callable[[dict[str, int]], None] = lambda usage: None
    _items: dict[str, dict] = field(default_factory=dict)
    _pending: dict[tuple[str, str, int], str] = field(default_factory=dict)
    _content: dict[tuple[str, str, int], str] = field(default_factory=dict)
    _content_bytes: dict[str, int] = field(default_factory=dict)
    _truncated: set[str] = field(default_factory=set)
    _last_flush: float = field(default_factory=time.monotonic)
    _usage_total: dict[str, int] | None = None
    terminal_status: str = ""
    failure_detail: str = ""
    _plan: dict | None = None

    def bind_thread(self, native_id: str) -> None:
        self.thread_id = native_id

    def consume_notification(self, method: str, params: dict) -> None:
        if method == "mcpServer/startupStatus/updated" and params.get("name") == "chatcopilot":
            if params.get("status") == "failed":
                raise RuntimeError("Required Session Gateway failed to initialize")
        if params.get("threadId") != self.thread_id or not self.thread_id:
            return
        if method == "turn/started":
            incoming = (params.get("turn") or {}).get("id")
            if not self.turn_id and isinstance(incoming, str):
                self.turn_id = incoming
                # thread/start alone does not materialize a resumable rollout.
                self.on_thread_started(self.thread_id)
            elif incoming != self.turn_id:
                raise RuntimeError("Unexpected App Server turn")
            return
        incoming = (params.get("turn") or {}).get("id") if method == "turn/completed" else params.get("turnId")
        if incoming != self.turn_id or not self.turn_id or self.terminal_status:
            return
        if method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total")
            if isinstance(total, dict) and all(type(total.get(k)) is int and total[k] >= 0 for k in _TOKEN_KEYS if k != "cacheWriteInputTokens"):
                self._usage_total = {key: total.get(key, 0) for key in _TOKEN_KEYS}
                self.on_usage(self._usage_total)
            return
        if method == "turn/completed":
            self.terminal_status = str(params["turn"].get("status", "failed"))
            error = params["turn"].get("error")
            if isinstance(error, dict):
                self.failure_detail = str(error.get("message") or "")[:4000]
            self.flush(force=True)
            if self._plan is not None:
                self.on_event(SpanFinished(name="Codex plan", kind="plan", ok=self.terminal_status == "completed",
                    trace_id=self.trace_id, span_id=_item_span_id(self.trace_id, self.turn_id + ":plan"),
                    parent_span_id=self.llm_span_id, depth=1, backend="codex", source="provider", data={"output": self._plan}))
            usage = None
            if self._usage_total is not None and self.initial_usage is not None:
                if all(self._usage_total[k] >= self.initial_usage.get(k, 0) for k in _TOKEN_KEYS):
                    usage = {target: self._usage_total[key] - self.initial_usage.get(key, 0)
                             for key, target in _TOKEN_KEYS.items()}
            ok = self.terminal_status == "completed"
            self.provider_failed = not ok
            self._remember_terminal(finish_reason=self.terminal_status, usage=usage, ok=ok)
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict):
                self._item(item, completed=method == "item/completed")
        elif method in _DELTAS:
            identity, delta = params.get("itemId"), params.get("delta")
            if isinstance(identity, str) and isinstance(delta, str):
                section = params.get("summaryIndex", 0)
                if type(section) is int and 0 <= section <= 1024:
                    self._delta(identity, _DELTAS[method], section, delta)
        elif method == "turn/plan/updated":
            identity = self.turn_id + ":plan"
            span_id = _item_span_id(self.trace_id, identity)
            if self._plan is None:
                self.on_event(SpanStarted(name="Codex plan", kind="plan", trace_id=self.trace_id,
                    span_id=span_id, parent_span_id=self.llm_span_id, depth=1, backend="codex", source="provider"))
            self._plan = {"plan": params.get("plan"), "explanation": params.get("explanation")}
            self.on_event(SpanUpdated(name="Codex plan", kind="plan", trace_id=self.trace_id,
                span_id=span_id, parent_span_id=self.llm_span_id, revision=self._revision(identity),
                depth=1, backend="codex", source="provider", data={"output": self._plan}))
        self.flush()

    def _item(self, item: dict, *, completed: bool) -> None:
        identity, item_type = item.get("id"), item.get("type")
        if not isinstance(identity, str) or not identity or item_type not in _ITEMS:
            return
        if identity in self._completed_items or identity in self._message_completed:
            return
        native_type, kind = _ITEMS[item_type]
        # Only selected public fields cross the protocol boundary; reasoning.content is excluded.
        public = {key: item[key] for key in ("id", "text", "phase", "summary", "command", "cwd",
            "server", "tool", "arguments", "result", "error", "changes", "query", "action") if key in item}
        public.update(type=native_type, status=item.get("status", "completed" if completed else "in_progress"),
                      aggregated_output=item.get("aggregatedOutput"), exit_code=item.get("exitCode"))
        if native_type == "reasoning":
            public.pop("text", None)
        if native_type == "plan_update":
            public["plan"] = item.get("text")
        if native_type == "subagent":
            public.update(prompt=item.get("prompt"), receiver_thread_ids=item.get("receiverThreadIds"),
                          agents_states=item.get("agentsStates"))
        if identity not in self._items and len(self._items) >= 500:
            if completed and kind == "message" and item.get("phase") != "commentary" and item.get("text"):
                self.final_parts.append(item["text"])
            self._mark_provider_item_omitted(identity, from_completion=completed)
            return
        known = identity in self._items
        self._items[identity] = {"phase": public.get("phase"), "type": native_type}
        if not completed:
            if kind == "message" and not known:
                # Persist the actual item start before buffered text, so a later
                # tool or summary cannot overtake this message in the timeline.
                self.on_event(AgentMessageObserved(text="", message_id=identity, trace_id=self.trace_id,
                    span_id=_item_span_id(self.trace_id, identity), parent_span_id=self.llm_span_id,
                    revision=self._revision(identity), phase="start", status="running", backend="codex",
                    source="provider", depth=1, observed_at=time.time(),
                    message_kind="progress" if item.get("phase") == "commentary" else "response"))
                if isinstance(item.get("text"), str) and item["text"]:
                    self._delta(identity, "message", 0, item["text"])
            elif kind != "message":
                self._start_item(identity, public, native_type, kind)
            return
        self.flush(force=True)
        if kind == "message":
            self._message_item(identity, public, completed=True)
        else:
            if kind == "subagent":
                self.on_event(SpanFinished(name="subagent:" + str(item.get("tool", "Codex")), kind=kind,
                    ok=item.get("status") not in {"failed", "cancelled"}, trace_id=self.trace_id,
                    span_id=_item_span_id(self.trace_id, identity), parent_span_id=self.llm_span_id,
                    depth=1, backend="codex", source="provider", data={"input": {"prompt": item.get("prompt")},
                        "output": {"agents_states": item.get("agentsStates"), "thread_ids": item.get("receiverThreadIds")}}))
                self._active_spans.pop(identity, None)
                self._completed_items.add(identity)
            else:
                self._finish_item(identity, public, native_type, kind)
        for key in [key for key in self._content if key[0] == identity]:
            self._content.pop(key)

    def _message_item(self, identity: str, item: dict, *, completed: bool) -> None:
        text = item.get("text") or ""
        phase = item.get("phase")
        message_kind = "progress" if phase == "commentary" else "final" if phase == "final_answer" else "response"
        if completed:
            self._message_completed.add(identity)
            if phase != "commentary" and text:
                self.final_parts.append(text)
        raw = text.encode("utf-8")
        self.on_event(AgentMessageObserved(text=raw[:_CONTENT_BYTES].decode("utf-8", errors="ignore"),
            message_id=identity, trace_id=self.trace_id, span_id=_item_span_id(self.trace_id, identity),
            parent_span_id=self.llm_span_id, revision=self._revision(identity),
            phase="finish" if completed else "update", message_kind=message_kind,
            backend="codex", source="provider", depth=1, observed_at=time.time(),
            capture_state="truncated" if len(raw) > _CONTENT_BYTES else "available"))

    def _delta(self, identity: str, content_kind: str, section: int, delta: str) -> None:
        if identity in self._completed_items or identity in self._message_completed:
            return
        if identity not in self._items:
            return
        remaining = max(0, _CONTENT_BYTES - self._content_bytes.get(identity, 0))
        raw = delta.encode("utf-8")
        if len(raw) > remaining:
            self._truncated.add(identity)
        text = raw[:remaining].decode("utf-8", errors="ignore")
        self._content_bytes[identity] = self._content_bytes.get(identity, 0) + len(text.encode("utf-8"))
        key = (identity, content_kind, section)
        self._pending[key] = self._pending.get(key, "") + text
        self._content[key] = self._content.get(key, "") + text

    def flush(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_flush < .25:
            return
        self._last_flush = time.monotonic()
        for (identity, kind, section), text in self._pending.items():
            if not text:
                continue
            item = self._items[identity]
            self.on_event(AgentContentDelta(text=text, item_id=identity, content_kind=kind,
                trace_id=self.trace_id, span_id=_item_span_id(self.trace_id, identity),
                parent_span_id=self.llm_span_id, revision=self._revision(identity), section=section,
                message_kind="progress" if item.get("phase") == "commentary" else "response",
                observed_at=time.time(), capture_state="truncated" if identity in self._truncated else "available"))
        self._pending.clear()

    def fail(self, *, reason: str = "backend_failed") -> None:
        self.flush(force=True)
        super().fail(reason=reason)

    def _close_active_spans(self, *, ok: bool, summary: str) -> None:
        self.flush(force=True)
        status = "cancelled" if "cancel" in summary else "incomplete"
        for identity, item in self._items.items():
            if identity in self._completed_items or identity in self._message_completed:
                continue
            sections = sorted((key[2], text) for key, text in self._content.items() if key[0] == identity)
            text = "\n\n".join(text for _, text in sections)
            capture = "truncated" if identity in self._truncated else "available" if text else "not_recorded"
            if item["type"] == "agent_message":
                self.on_event(AgentMessageObserved(text=text, message_id=identity,
                    trace_id=self.trace_id, span_id=_item_span_id(self.trace_id, identity),
                    parent_span_id=self.llm_span_id, revision=self._revision(identity),
                    status=status, backend="codex", source="provider", depth=1, capture_state=capture))
                self._message_completed.add(identity)
            else:
                active = self._active_spans.pop(identity, None)
                if active is not None:
                    output = {"public_summary": [text]} if active.kind == "reasoning" else {"aggregated_output": text}
                    self.on_event(SpanFinished(name=active.name, kind=active.kind, ok=False,
                        trace_id=self.trace_id, span_id=active.span_id, parent_span_id=self.llm_span_id,
                        depth=1, backend="codex", source="provider", summary=summary,
                        data={"output": output, "status": status, "capture_state": capture}))
                    self._completed_items.add(identity)
        super()._close_active_spans(ok=ok, summary=summary)
