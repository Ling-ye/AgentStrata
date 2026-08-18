"""Per-turn task progress records for the console UI."""
from __future__ import annotations

import contextvars
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from chatcopilot.contracts.identity import stable_actor_ref
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.runtime.jobs.notification import read_json_file, write_json_atomic
from chatcopilot.middleware.runtime.task_forecast import (
    FORECAST_VERSION,
    forecast_llm_usage,
    forecast_task_usage,
    load_task_history,
    normalize_usage,
)
from chatcopilot.middleware.runtime.workspace import Workspace

TASK_SCHEMA_VERSION = 2
TASKS_DIRNAME = "tasks"
TASK_FILENAME = "task.json"
EVENTS_FILENAME = "events.jsonl"
TURN_FILENAME = "turn.json"
_JOB_ID_RE = re.compile(r"\bjob_\d{8}_\d{6}_[0-9a-fA-F]{8}\b")


def make_task_id(now: Optional[float] = None) -> str:
    ts = time.localtime(time.time() if now is None else now)
    return f"task_{time.strftime('%Y%m%d_%H%M%S', ts)}_{uuid.uuid4().hex[:8]}"


def describe_user_text(text: str, *, limit: int = 120) -> str:
    first_line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    if not first_line:
        return "（空消息）"
    return first_line if len(first_line) <= limit else first_line[: limit - 1] + "…"


def _workspace_payload(workspace: Workspace) -> Dict[str, Any]:
    shared_group = workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
    return {
        "root": str(workspace.root),
        "chat_kind": workspace.chat_kind,
        "chat_id": workspace.chat_id,
        "user_id": None if shared_group else workspace.user_id,
        "user_name": None if shared_group else workspace.user_name,
        "actor_ref": (
            stable_actor_ref(
                "qq",
                workspace.user_id or "",
                conversation_id=f"{workspace.chat_kind or ''}:{workspace.chat_id or ''}",
            )
            if shared_group and workspace.user_id
            else None
        ),
    }


def _extract_job_ids(*parts: object) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    for part in parts:
        text = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False, default=str)
        for job_id in _JOB_ID_RE.findall(text):
            if job_id not in seen:
                found.append(job_id)
                seen.add(job_id)
    return found


def _empty_usage_totals() -> Dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "llm_calls": 0,
        "cache_hit_calls": 0,
        "cache_hit_rate": 0.0,
        "cache_hit_call_rate": 0.0,
    }


@dataclass
class TurnTaskRecorder:
    workspace: Workspace
    session_id: str
    message_id: Optional[str]
    user_text: str
    task_id: str = field(default_factory=make_task_id)
    asked_at: float = field(default_factory=time.time)
    history_root: Optional[Path] = None
    _path: Path = field(init=False, repr=False)
    _tools: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _llm_calls: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _usage_totals: Dict[str, Any] = field(default_factory=_empty_usage_totals, init=False, repr=False)
    _job_ids: List[str] = field(default_factory=list, init=False, repr=False)
    _status: str = field(default="running", init=False, repr=False)
    _progress: str = field(default="已收到提问。", init=False, repr=False)
    _finished_at: Optional[float] = field(default=None, init=False, repr=False)
    _turn_finished_at: Optional[float] = field(default=None, init=False, repr=False)
    _job_results: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _steps: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _primary_model: str = field(default="", init=False, repr=False)
    _context_kind: str = field(default="", init=False, repr=False)
    _forecast: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _log_context_token: Optional[contextvars.Token] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED:
            raise ValueError("shared-group turn diagnostics require protected storage")
        self._path = self.workspace.root / TASKS_DIRNAME / self.task_id / TASK_FILENAME
        self._forecast = {
            "status": "insufficient",
            "model": "",
            "context_kind": "",
            "sample_count": 0,
            "estimator_version": FORECAST_VERSION,
            "baseline": None,
            "fixed_at": None,
        }
        self.write(progress=self._progress)
        self._append_event("task_started", {"user_text": self.user_text})
        from chatcopilot.core.log_context import push_log_context

        self._log_context_token = push_log_context(
            task_id=self.task_id,
            trace_id=self.task_id,
            session_id=self.session_id,
        )

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        *,
        status: Optional[str] = None,
        progress: Optional[str] = None,
        finished_at: Optional[float] = None,
    ) -> None:
        if status is not None:
            self._status = status
        if progress is not None:
            self._progress = progress
        if finished_at is not None:
            self._finished_at = finished_at
        payload = self.to_payload()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._path, payload)

    def _find_step(self, span_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not span_id:
            return None
        return next(
            (step for step in reversed(self._steps) if step.get("step_id") == span_id),
            None,
        )

    def _start_step(
        self,
        *,
        step_id: Optional[str],
        step_type: str,
        title: str,
        parent_step_id: Optional[str],
        depth: int,
        started_at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        estimated_usage: Optional[Dict[str, Any]] = None,
        raw_event: str,
    ) -> Dict[str, Any]:
        resolved_id = step_id or f"{step_type}_{uuid.uuid4().hex[:12]}"
        existing = self._find_step(resolved_id)
        if existing is not None:
            if raw_event not in existing["raw_event_types"]:
                existing["raw_event_types"].append(raw_event)
            return existing
        step = {
            "step_id": resolved_id,
            "type": step_type,
            "parent_step_id": parent_step_id,
            "depth": max(0, int(depth)),
            "status": "running",
            "title": title,
            "started_at": started_at if started_at is not None else time.time(),
            "finished_at": None,
            "elapsed_s": None,
            "summary": "",
            "error": None,
            "metadata": dict(metadata or {}),
            "estimated_usage": normalize_usage(estimated_usage),
            "actual_usage": normalize_usage({}),
            "inclusive_usage": normalize_usage({}),
            "raw_event_types": [raw_event],
        }
        self._steps.append(step)
        return step

    def _finish_step(
        self,
        step: Dict[str, Any],
        *,
        ok: bool,
        summary: str = "",
        error: Optional[str] = None,
        finished_at: Optional[float] = None,
        actual_usage: Optional[Dict[str, Any]] = None,
        raw_event: str,
    ) -> None:
        ended = finished_at if finished_at is not None else time.time()
        step["status"] = "succeeded" if ok else "failed"
        step["finished_at"] = ended
        started_at = step.get("started_at")
        step["elapsed_s"] = (
            round(ended - float(started_at), 4)
            if isinstance(started_at, (int, float))
            else None
        )
        step["summary"] = summary or ""
        step["error"] = error
        if actual_usage is not None:
            step["actual_usage"] = normalize_usage(actual_usage)
        if raw_event not in step["raw_event_types"]:
            step["raw_event_types"].append(raw_event)
        self._refresh_inclusive_usage()

    def _refresh_inclusive_usage(self) -> None:
        by_parent: Dict[str, List[Dict[str, Any]]] = {}
        for step in self._steps:
            parent = step.get("parent_step_id")
            if parent:
                by_parent.setdefault(str(parent), []).append(step)

        def inclusive(step: Dict[str, Any], seen: set[str]) -> Dict[str, int]:
            step_id = str(step.get("step_id") or "")
            if not step_id or step_id in seen:
                return normalize_usage(step.get("actual_usage"))
            next_seen = {*seen, step_id}
            totals = normalize_usage(step.get("actual_usage"))
            for child in by_parent.get(step_id, []):
                child_usage = inclusive(child, next_seen)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "reasoning_tokens",
                    "cached_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                ):
                    totals[key] += child_usage[key]
            totals = normalize_usage(totals)
            step["inclusive_usage"] = totals
            return totals

        for item in self._steps:
            inclusive(item, set())

    def _accumulate_usage(self, usage: Dict[str, Any]) -> None:
        normalized = _normalize_usage_payload(usage)
        self._usage_totals["llm_calls"] += 1
        if normalized.get("cached_tokens", 0) > 0 or normalized.get("cache_read_tokens", 0) > 0:
            self._usage_totals["cache_hit_calls"] += 1
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            self._usage_totals[key] += int(normalized.get(key, 0) or 0)
        prompt_tokens = int(self._usage_totals["prompt_tokens"] or 0)
        llm_calls = int(self._usage_totals["llm_calls"] or 0)
        self._usage_totals["cache_hit_rate"] = (
            round(float(self._usage_totals["cached_tokens"]) / prompt_tokens, 4)
            if prompt_tokens > 0
            else 0.0
        )
        self._usage_totals["cache_hit_call_rate"] = (
            round(float(self._usage_totals["cache_hit_calls"]) / llm_calls, 4)
            if llm_calls > 0
            else 0.0
        )

    def tool_started(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        depth: int = 0,
    ) -> None:
        started_at = time.time()
        self._tools.append(
            {
                "name": name,
                "kind": "tool",
                "status": "running",
                "arguments": arguments,
                "started_at": started_at,
                "finished_at": None,
                "elapsed_s": None,
                "summary": "",
                "error": None,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "depth": depth,
            }
        )
        self._start_step(
            step_id=span_id,
            step_type="tool",
            title=name,
            parent_step_id=parent_span_id,
            depth=depth,
            started_at=started_at,
            metadata={"tool": name},
            raw_event="tool_started",
        )
        self._append_event("tool_started", self._tools[-1])
        # 仅在主 Agent 层（depth==0）刷新可见进度，避免 subagent 内部工具刷屏。
        if depth <= 0:
            self.write(progress=f"正在调用工具 {name}。")

    def record_job_submitted(self, job_id: str) -> None:
        normalized = str(job_id or "").strip()
        if not _JOB_ID_RE.fullmatch(normalized):
            raise ValueError(f"invalid background job id: {job_id}")
        if normalized not in self._job_ids:
            self._job_ids.append(normalized)
        self._append_event("job_submitted", {"job_id": normalized})
        self.write(progress=f"Background job submitted: {normalized}.")

    def tool_finished(
        self,
        name: str,
        ok: bool,
        summary: str,
        error: Optional[str] = None,
        *,
        span_id: Optional[str] = None,
        depth: int = 0,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        finished_at = time.time()
        target = self._match_running(name, span_id)
        if target is None:
            target = {"name": name, "kind": "tool", "started_at": None, "depth": depth}
            if span_id:
                target["span_id"] = span_id
            self._tools.append(target)
        target["status"] = "succeeded" if ok else "failed"
        target["finished_at"] = finished_at
        started_at = target.get("started_at")
        if isinstance(started_at, (int, float)):
            target["elapsed_s"] = round(finished_at - float(started_at), 1)
        target["summary"] = summary or ""
        target["error"] = error
        target["result"] = dict(data or {})
        step = self._find_step(span_id)
        if step is None:
            step = self._start_step(
                step_id=span_id,
                step_type="tool",
                title=name,
                parent_step_id=None,
                depth=depth,
                started_at=target.get("started_at"),
                metadata={"tool": name},
                raw_event="tool_started",
            )
        self._finish_step(
            step,
            ok=ok,
            summary=summary,
            error=error,
            finished_at=finished_at,
            raw_event="tool_finished",
        )
        self._append_event("tool_finished", target)
        for job_id in _extract_job_ids(summary, error):
            if job_id not in self._job_ids:
                self._job_ids.append(job_id)
        if depth <= 0:
            progress = f"工具 {name} 调用完成。" if ok else f"工具 {name} 调用失败。"
            self.write(progress=progress)

    def _match_running(self, name: str, span_id: Optional[str]):
        if span_id:
            for tool in reversed(self._tools):
                if tool.get("span_id") == span_id and tool.get("status") == "running":
                    return tool
        return next(
            (
                tool
                for tool in reversed(self._tools)
                if tool.get("name") == name and tool.get("status") == "running"
            ),
            None,
        )

    def span_started(
        self,
        name: str,
        kind: str,
        *,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        depth: int = 0,
    ) -> None:
        started_at = time.time()
        self._tools.append(
            {
                "name": name,
                "kind": kind,
                "status": "running",
                "started_at": started_at,
                "finished_at": None,
                "elapsed_s": None,
                "summary": "",
                "error": None,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "depth": depth,
            }
        )
        self._start_step(
            step_id=span_id,
            step_type=kind,
            title=name,
            parent_step_id=parent_span_id,
            depth=depth,
            started_at=started_at,
            raw_event="span_started",
        )
        self._append_event("span_started", self._tools[-1])
        self.write(progress=f"委托 {name} 处理中。")

    def span_finished(
        self,
        name: str,
        kind: str,
        ok: bool,
        summary: str,
        *,
        span_id: Optional[str] = None,
        depth: int = 0,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        finished_at = time.time()
        if kind == "llm":
            step = self._find_step(span_id)
            if step is not None:
                self._finish_step(
                    step,
                    ok=ok,
                    summary=summary,
                    error=None if ok else summary,
                    finished_at=finished_at,
                    raw_event="llm_call_failed" if not ok else "span_finished",
                )
                self._append_event(
                    "llm_call_failed" if not ok else "span_finished",
                    {
                        "name": name,
                        "kind": kind,
                        "ok": ok,
                        "summary": summary,
                        "span_id": span_id,
                        "depth": depth,
                        "finished_at": finished_at,
                    },
                )
                self.write(progress="模型调用失败。" if not ok else "模型调用完成。")
                return
        target = self._match_running(name, span_id)
        if target is None:
            target = {"name": name, "kind": kind, "started_at": None, "depth": depth}
            if span_id:
                target["span_id"] = span_id
            self._tools.append(target)
        target["status"] = "succeeded" if ok else "failed"
        target["finished_at"] = finished_at
        started_at = target.get("started_at")
        if isinstance(started_at, (int, float)):
            target["elapsed_s"] = round(finished_at - float(started_at), 1)
        target["summary"] = summary or ""
        step = self._find_step(span_id)
        if step is None:
            step = self._start_step(
                step_id=span_id,
                step_type=kind,
                title=name,
                parent_step_id=None,
                depth=depth,
                started_at=target.get("started_at"),
                raw_event="span_started",
            )
        self._finish_step(
            step,
            ok=ok,
            summary=summary,
            finished_at=finished_at,
            raw_event="span_finished",
        )
        transcript = (data or {}).get("transcript") if data else None
        if transcript and span_id:
            transcript_path = self._persist_subagent_transcript(span_id, name, data or {})
            if transcript_path is not None:
                target["transcript_path"] = str(transcript_path)
        self._append_event("span_finished", target)
        self.write(progress=f"委托 {name} 完成。")

    def llm_call_started(
        self,
        *,
        model: str,
        iteration: int,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        depth: int = 0,
        input_message_count: int = 0,
        input_estimated_tokens: int = 0,
        system_estimated_tokens: int = 0,
        tool_schema_count: int = 0,
        tool_schema_estimated_tokens: int = 0,
        estimator_version: str = "",
        context_kind: str = "",
    ) -> None:
        history = load_task_history(self.history_root)
        role = "main" if depth <= 0 else "subagent"
        step_forecast = forecast_llm_usage(
            history,
            model=model,
            context_kind=context_kind,
            role=role,
            rough_input_tokens=input_estimated_tokens,
        )
        if not self._primary_model:
            self._primary_model = model
            self._context_kind = context_kind
        if (
            self._forecast.get("status") != "ready"
            and model == self._primary_model
            and context_kind == self._context_kind
        ):
            next_forecast = forecast_task_usage(
                history,
                model=model,
                context_kind=context_kind,
            )
            next_forecast["fixed_at"] = (
                time.time() if next_forecast.get("status") == "ready" else None
            )
            self._forecast = next_forecast
        call = {
            "model": model,
            "iteration": iteration,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "depth": depth,
            "role": role,
            "started_at": time.time(),
            "input_message_count": input_message_count,
            "input_estimated_tokens": input_estimated_tokens,
            "raw_input_estimated_tokens": input_estimated_tokens,
            "system_estimated_tokens": system_estimated_tokens,
            "tool_schema_count": tool_schema_count,
            "tool_schema_estimated_tokens": tool_schema_estimated_tokens,
            "estimator_version": estimator_version,
            "context_kind": context_kind,
            "forecast": step_forecast,
        }
        step = self._start_step(
            step_id=span_id,
            step_type="llm",
            title=f"{model or 'LLM'} · 第 {iteration + 1} 轮",
            parent_step_id=parent_span_id,
            depth=depth,
            started_at=call["started_at"],
            metadata={
                "model": model,
                "iteration": iteration,
                "role": role,
                "context_kind": context_kind,
                "forecast_status": step_forecast["status"],
                "sample_count": step_forecast["sample_count"],
                "estimator_version": estimator_version,
                "raw_input_estimated_tokens": input_estimated_tokens,
                "system_estimated_tokens": system_estimated_tokens,
                "tool_schema_estimated_tokens": tool_schema_estimated_tokens,
            },
            estimated_usage=step_forecast["usage"],
            raw_event="llm_call_started",
        )
        call["step_id"] = step["step_id"]
        self._append_event("llm_call_started", call)
        self.write(progress=f"正在调用模型 {model or 'LLM'}。")

    def llm_call_finished(
        self,
        *,
        model: str,
        iteration: int,
        finish_reason: str = "",
        usage: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        depth: int = 0,
        input_message_count: int = 0,
        input_estimated_tokens: int = 0,
        system_estimated_tokens: int = 0,
        tool_schema_count: int = 0,
        tool_schema_estimated_tokens: int = 0,
        estimator_version: str = "",
        context_kind: str = "",
    ) -> None:
        normalized = _normalize_usage_payload(usage or {})
        effective_span_id = span_id
        existing_step = self._find_step(span_id)
        if existing_step is not None and existing_step.get("status") != "running":
            effective_span_id = f"{span_id}:{iteration}" if span_id else None
        call = {
            "model": model,
            "iteration": iteration,
            "finish_reason": finish_reason,
            "usage": normalized,
            "trace_id": trace_id,
            "span_id": effective_span_id,
            "parent_span_id": parent_span_id,
            "depth": depth,
            "role": "main" if depth <= 0 else "subagent",
            "recorded_at": time.time(),
            "input_message_count": input_message_count,
            "input_estimated_tokens": input_estimated_tokens,
            "raw_input_estimated_tokens": input_estimated_tokens,
            "system_estimated_tokens": system_estimated_tokens,
            "tool_schema_count": tool_schema_count,
            "tool_schema_estimated_tokens": tool_schema_estimated_tokens,
            "estimator_version": estimator_version,
            "context_kind": context_kind,
        }
        step = self._find_step(effective_span_id)
        if step is None:
            self.llm_call_started(
                model=model,
                iteration=iteration,
                trace_id=trace_id,
                span_id=effective_span_id,
                parent_span_id=parent_span_id,
                depth=depth,
                input_message_count=input_message_count,
                input_estimated_tokens=input_estimated_tokens,
                system_estimated_tokens=system_estimated_tokens,
                tool_schema_count=tool_schema_count,
                tool_schema_estimated_tokens=tool_schema_estimated_tokens,
                estimator_version=estimator_version,
                context_kind=context_kind,
            )
            step = self._find_step(effective_span_id)
        self._llm_calls.append(call)
        self._append_event("llm_call_finished", call)
        if step is not None:
            self._finish_step(
                step,
                ok=True,
                summary=finish_reason or "模型调用完成",
                finished_at=call["recorded_at"],
                actual_usage=normalized,
                raw_event="llm_call_finished",
            )
        self._accumulate_usage(normalized)
        self.write()

    def topic_decision(
        self,
        *,
        decision: str,
        context_kind: str,
        confidence: float,
        reason: str,
        source: str,
        model: str = "",
        usage: Optional[Dict[str, Any]] = None,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
        elapsed_s: Optional[float] = None,
    ) -> None:
        ended = finished_at if finished_at is not None else time.time()
        started = started_at if started_at is not None else ended
        step = self._start_step(
            step_id=f"routing_{uuid.uuid4().hex[:12]}",
            step_type="routing",
            title="上下文路由",
            parent_step_id=None,
            depth=0,
            started_at=started,
            metadata={
                "decision": decision,
                "context_kind": context_kind,
                "confidence": confidence,
                "source": source,
                "model": model,
            },
            raw_event="topic_decision",
        )
        self._finish_step(
            step,
            ok=True,
            summary=reason,
            finished_at=ended,
            actual_usage=usage,
            raw_event="topic_decision",
        )
        if elapsed_s is not None:
            step["elapsed_s"] = max(0.0, float(elapsed_s))
        if usage:
            normalized = _normalize_usage_payload(usage)
            self._llm_calls.append(
                {
                    "kind": "routing",
                    "model": model,
                    "iteration": -1,
                    "finish_reason": "decision",
                    "usage": normalized,
                    "role": "main",
                    "recorded_at": ended,
                    "context_kind": context_kind,
                    "span_id": step["step_id"],
                }
            )
            self._accumulate_usage(normalized)
        self._append_event(
            "topic_decision",
            {
                "decision": decision,
                "context_kind": context_kind,
                "confidence": confidence,
                "reason": reason,
                "source": source,
                "model": model,
                "usage": usage or {},
                "started_at": started,
                "finished_at": ended,
                "elapsed_s": step["elapsed_s"],
                "step_id": step["step_id"],
            },
        )
        self.write(
            progress=(
                "话题判定："
                f"{decision} -> {context_kind} "
                f"(source={source}, confidence={confidence:.2f})，原因：{reason}"
            )
        )

    def _persist_subagent_transcript(
        self, span_id: str, name: str, data: Dict[str, Any]
    ) -> Optional[Path]:
        try:
            target_dir = self._path.parent / "subagents"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{span_id}.json"
            write_json_atomic(
                target,
                {
                    "name": name,
                    "span_id": span_id,
                    "stop_reason": data.get("stop_reason"),
                    "result": data.get("result"),
                    "transcript": data.get("transcript"),
                },
            )
            return target
        except Exception:  # noqa: BLE001
            return None

    def finish(
        self,
        *,
        status: str,
        progress: str,
        final_text: str = "",
        stop_reason: str = "",
        error: str = "",
        produced_resources: Optional[List[str]] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
    ) -> None:
        turn_finished_at = time.time()
        for step in self._steps:
            if step.get("status") == "running":
                self._finish_step(
                    step,
                    ok=status in {"succeeded", "delegated"},
                    summary=(
                        "任务已转交后台执行"
                        if status == "delegated"
                        else ("任务结束" if status == "succeeded" else error or progress)
                    ),
                    error=None if status in {"succeeded", "delegated"} else error or progress,
                    finished_at=turn_finished_at,
                    raw_event="task_finished",
                )
        self._turn_finished_at = turn_finished_at
        turn = {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "user_text": self.user_text,
            "final_text": final_text,
            "stop_reason": stop_reason,
            "error": error,
            "produced_resources": list(produced_resources or []),
            "turn_finished_at": turn_finished_at,
            "finished_at": None if status == "delegated" else turn_finished_at,
        }
        if lifecycle:
            turn.update(lifecycle)
        try:
            write_json_atomic(self._path.parent / TURN_FILENAME, turn)
            if status == "delegated":
                self._append_event("task_delegated", turn)
                self.write(status=status, progress=progress)
            else:
                self._append_event("task_finished", turn)
                self.write(status=status, progress=progress, finished_at=turn_finished_at)
        finally:
            if self._log_context_token is not None:
                from chatcopilot.core.log_context import pop_log_context

                pop_log_context(self._log_context_token)
                self._log_context_token = None

    def record_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        self._append_event(event_type, payload)

    def _append_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        target = self._path.parent / EVENTS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        event = {"event": event_type, "recorded_at": time.time(), "data": payload}
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def to_payload(self) -> Dict[str, Any]:
        finished_at = self._finished_at
        elapsed_s = None
        if finished_at is not None:
            elapsed_s = round(finished_at - self.asked_at, 1)
        updated_at = finished_at or time.time()
        current = next(
            (step for step in reversed(self._steps) if step.get("status") == "running"),
            self._steps[-1] if self._steps else None,
        )
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "description": describe_user_text(self.user_text),
            "progress": self._progress,
            "status": self._status,
            "submitter": (
                stable_actor_ref(
                    "qq",
                    self.workspace.user_id or "",
                    conversation_id=(
                        f"{self.workspace.chat_kind or ''}:{self.workspace.chat_id or ''}"
                    ),
                )
                if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
                else self.workspace.user_name or self.workspace.user_id or ""
            ),
            "asked_at": self.asked_at,
            "started_at": self.asked_at,
            "finished_at": finished_at,
            "turn_finished_at": self._turn_finished_at,
            "elapsed_s": elapsed_s,
            "updated_at": updated_at,
            "tools": self._tools,
            "llm_calls": self._llm_calls,
            "steps": self._steps,
            "current_step": current.get("title") if current else self._progress,
            "usage_totals": dict(self._usage_totals),
            "forecast": dict(self._forecast),
            "primary_model": self._primary_model,
            "context_kind": self._context_kind,
            "job_ids": self._job_ids,
            "job_results": self._job_results,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "workspace": _workspace_payload(self.workspace),
            "path": str(self._path.parent),
            "trace_id": self.task_id,
            "events_path": str(self._path.parent / EVENTS_FILENAME),
            "turn_path": str(self._path.parent / TURN_FILENAME),
        }


def complete_delegated_task(
    workspace: Workspace,
    *,
    task_id: str,
    job_id: str,
    result: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Merge one child result and terminalize a delegated parent when all children finish."""

    if not str(task_id or "").startswith("task_") or "/" in task_id or "\\" in task_id:
        return None
    task_dir = workspace.root / TASKS_DIRNAME / task_id
    task_path = task_dir / TASK_FILENAME
    task = read_json_file(task_path)
    if not isinstance(task, dict):
        return None

    summaries = {
        str(item.get("job_id") or ""): dict(item)
        for item in task.get("job_results") or []
        if isinstance(item, dict) and item.get("job_id")
    }
    if job_id in summaries and task.get("status") in {"succeeded", "failed"}:
        return task
    summaries[job_id] = _job_result_summary(job_id, result)
    ordered_ids = [str(item) for item in task.get("job_ids") or [] if str(item)]
    if job_id not in ordered_ids:
        ordered_ids.append(job_id)
    ordered_results = [summaries[item] for item in ordered_ids if item in summaries]

    now = time.time()
    task["job_ids"] = ordered_ids
    task["job_results"] = ordered_results
    task["updated_at"] = now
    task["progress"] = _delegated_progress(ordered_results, len(ordered_ids))

    all_complete = len(ordered_results) == len(ordered_ids)
    if all_complete:
        succeeded = all(bool(item.get("ok")) for item in ordered_results)
        task["status"] = "succeeded" if succeeded else "failed"
        task["finished_at"] = now
        started_at = task.get("started_at") or task.get("asked_at")
        if isinstance(started_at, (int, float)):
            task["elapsed_s"] = round(now - float(started_at), 1)
    else:
        task["status"] = "delegated"
        task["finished_at"] = None
        task["elapsed_s"] = None
    write_json_atomic(task_path, task)

    turn_path = task_dir / TURN_FILENAME
    turn = read_json_file(turn_path) or {}
    if isinstance(turn, dict):
        turn["job_results"] = ordered_results
        turn["status"] = task["status"]
        turn["finished_at"] = now if all_complete else None
        if all_complete:
            turn["produced_resources"] = [
                output
                for item in ordered_results
                for output in item.get("outputs") or []
                if isinstance(output, str)
            ]
            if not all(bool(item.get("ok")) for item in ordered_results):
                turn["error"] = "\n".join(
                    str(item.get("error") or "")
                    for item in ordered_results
                    if not item.get("ok")
                ).strip()
        write_json_atomic(turn_path, turn)

    _append_task_event(
        task_dir,
        "job_completed",
        {"job_id": job_id, "result": summaries[job_id]},
    )
    if all_complete:
        _append_task_event(
            task_dir,
            "task_finished",
            {
                "status": task["status"],
                "job_results": ordered_results,
                "finished_at": now,
            },
        )
    return task


def _job_result_summary(job_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    return {
        "job_id": job_id,
        "ok": bool(result.get("ok")),
        "status": "succeeded" if result.get("ok") else "failed",
        "stage": str(
            details.get("failed_stage")
            or result.get("stage")
            or ("succeeded" if result.get("ok") else "failed")
        ),
        "error_code": str(result.get("error_code") or ""),
        "summary": str(result.get("summary") or ""),
        "error": str(result.get("error") or ""),
        "outputs": [
            str(item)
            for item in result.get("outputs") or []
            if isinstance(item, str)
        ],
        "finished_at": result.get("finished_at"),
    }


def _delegated_progress(results: List[Dict[str, Any]], expected: int) -> str:
    completed = len(results)
    failed = sum(1 for item in results if not item.get("ok"))
    if completed < expected:
        return f"Background child jobs completed: {completed}/{expected}."
    if failed:
        return f"{failed} background child job(s) failed."
    return f"All {expected} background child job(s) completed."


def _append_task_event(task_dir: Path, event_type: str, payload: Dict[str, Any]) -> None:
    target = task_dir / EVENTS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    event = {"event": event_type, "recorded_at": time.time(), "data": payload}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _normalize_usage_payload(usage: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, bool):
            out[key] = 0
        elif isinstance(value, (int, float)):
            out[key] = int(value)
        elif isinstance(value, str):
            try:
                out[key] = int(float(value))
            except ValueError:
                out[key] = 0
        else:
            out[key] = 0
    return out


__all__ = [
    "TASK_SCHEMA_VERSION",
    "TASK_FILENAME",
    "TASKS_DIRNAME",
    "TurnTaskRecorder",
    "complete_delegated_task",
    "describe_user_text",
    "make_task_id",
]
