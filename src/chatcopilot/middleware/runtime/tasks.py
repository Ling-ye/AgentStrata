"""Turn progress and completion service; storage and projections have separate owners."""
from __future__ import annotations
import contextvars
import hashlib
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from chatcopilot.contracts.identity import stable_actor_ref
from chatcopilot.contracts.persona_control import PersonaDraftResult
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets,
    default_observability_roots,
    omit_local_resource_paths,
    omit_private_reasoning_messages,
    redact_observability_payload,
)
from chatcopilot.middleware.runtime.task_forecast import (
    FORECAST_VERSION,
    forecast_llm_usage,
    forecast_task_usage,
)
from chatcopilot.core.workspace_runtime import Workspace
from . import task_projection as _projection
from . import task_storage as _storage
from .task_projection import (
    MAX_PROVIDER_ACTIVITY_RAW_EVENTS as MAX_PROVIDER_ACTIVITY_RAW_EVENTS,
    TASKS_DIRNAME as TASKS_DIRNAME,
    TASK_FILENAME as TASK_FILENAME,
    TASK_SCHEMA_VERSION as TASK_SCHEMA_VERSION,
    describe_user_text as describe_user_text,
)
from .task_storage import (
    group_task_actor_root as group_task_actor_root,
    group_task_intake_root as group_task_intake_root,
)


_LOGGER = logging.getLogger(__name__)


def make_task_id(now: Optional[float] = None) -> str:
    ts = time.localtime(time.time() if now is None else now)
    return f"task_{time.strftime('%Y%m%d_%H%M%S', ts)}_{uuid.uuid4().hex[:8]}"


@dataclass
class TurnTaskRecorder:
    workspace: Workspace
    session_id: str
    message_id: Optional[str]
    user_text: str
    task_id: str = field(default_factory=make_task_id)
    asked_at: float = field(default_factory=time.time)
    history_root: Optional[Path] = None
    unauthenticated_intake: bool = False
    redact_identity: bool = False
    _path: Path = field(init=False, repr=False)
    _observability_root: Path = field(init=False, repr=False)
    _tools: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _llm_calls: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _context_snapshots: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _input_resources: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _usage_totals: Dict[str, Any] = field(
        default_factory=_projection._empty_usage_totals, init=False, repr=False
    )
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
    _persona_outcome: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _log_context_token: Optional[contextvars.Token] = field(default=None, init=False, repr=False)
    _event_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_summary_write_at: float = field(default=0.0, init=False, repr=False)
    _provider_activity_total: int = field(default=0, init=False, repr=False)
    _provider_activity_dropped: int = field(default=0, init=False, repr=False)
    _provider_omission_event_written: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._observability_root = _storage._resolve_task_observability_root(
            self.workspace,
            self.history_root,
        )
        if self.unauthenticated_intake and self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED:
            storage_root = _storage.group_task_intake_root(self.workspace, create=True)
        else:
            if self.workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
                _storage._materialize_private_task_workspace(
                    self.workspace,
                    history_root=self.history_root,
                    observability_root=self._observability_root,
                )
            storage_root = _storage.group_task_actor_root(
                self.workspace,
                create=self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED,
            )
        self._path = storage_root / _projection.TASKS_DIRNAME / self.task_id / _projection.TASK_FILENAME
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
        self._append_event(
            "task_started",
            {"user_text": self._persisted_user_text()},
        )
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
        with _storage._task_completion_lock(self._path.parent, create=True):
            self._apply_write_state(
                status=status,
                progress=progress,
                finished_at=finished_at,
            )
            self._merge_persisted_completion_state()
            self._write_task_summary_locked()

    def _apply_write_state(
        self,
        *,
        status: Optional[str],
        progress: Optional[str],
        finished_at: Optional[float],
    ) -> None:
        if status is not None:
            self._status = status
        if progress is not None:
            self._progress = progress
        if finished_at is not None:
            self._finished_at = finished_at

    def _merge_persisted_completion_state(self) -> None:
        """Merge completion-owned fields while holding ``.completion.lock``.

        The background watcher may finish before the main turn has emitted the
        corresponding ``ToolFinished`` event or final task summary.  Its
        persisted job result is authoritative and must not be replaced by this
        recorder's older in-memory view.
        """

        persisted = _storage._read_private_task_json(self._path.parent, _projection.TASK_FILENAME)
        if not isinstance(persisted, dict):
            return

        ordered_ids = [
            job_id
            for job_id in self._job_ids
            if isinstance(job_id, str) and _projection._JOB_ID_RE.fullmatch(job_id)
        ]
        for raw_job_id in persisted.get("job_ids") or []:
            job_id = str(raw_job_id or "")
            if _projection._JOB_ID_RE.fullmatch(job_id) and job_id not in ordered_ids:
                ordered_ids.append(job_id)

        summaries: Dict[str, Dict[str, Any]] = {}
        for source in (self._job_results, persisted.get("job_results") or []):
            if not isinstance(source, list):
                continue
            for raw_summary in source:
                if not isinstance(raw_summary, dict):
                    continue
                job_id = str(raw_summary.get("job_id") or "")
                if not _projection._JOB_ID_RE.fullmatch(job_id):
                    continue
                summaries[job_id] = dict(raw_summary)
                if job_id not in ordered_ids:
                    ordered_ids.append(job_id)

        self._job_ids = ordered_ids
        self._job_results = [summaries[job_id] for job_id in ordered_ids if job_id in summaries]

        all_complete = bool(ordered_ids) and all(job_id in summaries for job_id in ordered_ids)
        persisted_status = str(persisted.get("status") or "")
        if not all_complete or persisted_status not in {"succeeded", "failed"}:
            return

        # A failed main turn must not be hidden by a successful child.  In all
        # other cases the completion-owned terminal state is newer and wins.
        adopt_persisted_status = persisted_status == "failed" or self._status != "failed"
        if not adopt_persisted_status:
            return
        self._status = persisted_status
        persisted_progress = persisted.get("progress")
        if isinstance(persisted_progress, str) and persisted_progress:
            self._progress = persisted_progress
        persisted_finished_at = persisted.get("finished_at")
        if (
            isinstance(persisted_finished_at, (int, float))
            and not isinstance(persisted_finished_at, bool)
            and math.isfinite(float(persisted_finished_at))
        ):
            self._finished_at = float(persisted_finished_at)

    def _write_task_summary_locked(self) -> None:
        payload = self.to_payload()
        safe_payload = self._sanitize_for_persistence(
            payload,
            retain_group_current_text=True,
        )
        _storage._write_private_task_json(
            self._path.parent,
            _projection.TASK_FILENAME,
            safe_payload,
            create=True,
        )
        self._last_summary_write_at = time.monotonic()

    def _write_activity_progress(self, progress: str) -> None:
        """Bound task.json rewrites while raw activity events remain append-only."""
        self._progress = progress
        if time.monotonic() - self._last_summary_write_at < _projection.ACTIVITY_SUMMARY_WRITE_INTERVAL_SECONDS:
            return
        self.write()

    def _sanitize_for_persistence(
        self,
        payload: Any,
        *,
        retain_group_current_text: bool = False,
    ) -> Any:
        retained: Dict[str, Any] = {}
        if (
            retain_group_current_text
            and not self.redact_identity
            and self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
            and isinstance(payload, dict)
        ):
            expected = {
                "description": _projection.describe_user_text(self.user_text),
                "user_text": self.user_text,
            }
            retained = {
                key: value
                for key, value in expected.items()
                if payload.get(key) == value
            }
        prepared = payload
        if not self.redact_identity:
            prepared = _projection._redact_group_turn_content(
                payload,
                self.workspace,
                user_text=self.user_text,
                message_id=self.message_id,
            )
        if retained and isinstance(prepared, dict):
            prepared = {**prepared, **retained}
        if self.redact_identity and self.message_id:
            prepared = _projection._replace_identity_literals(prepared, (self.message_id,))
        result = redact_observability_payload(
            prepared,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(self._observability_root),
        )
        return _projection._redact_workspace_identity(
            result.value,
            self.workspace,
            force=self.redact_identity,
        )

    def _persisted_user_text(self) -> str:
        return self.user_text

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
            "estimated_usage": _projection.normalize_usage(estimated_usage),
            "actual_usage": _projection.normalize_usage({}),
            "inclusive_usage": _projection.normalize_usage({}),
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
            round(ended - float(started_at), 4) if isinstance(started_at, (int, float)) else None
        )
        step["summary"] = summary or ""
        step["error"] = error
        if actual_usage is not None:
            step["actual_usage"] = _projection.normalize_usage(actual_usage)
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
                return _projection.normalize_usage(step.get("actual_usage"))
            next_seen = {*seen, step_id}
            totals = _projection.normalize_usage(step.get("actual_usage"))
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
                    totals[key] = _projection._saturating_nonnegative_add(
                        totals.get(key, 0),
                        child_usage.get(key, 0),
                    )
            totals = _projection.normalize_usage(totals)
            step["inclusive_usage"] = totals
            return totals

        for item in self._steps:
            inclusive(item, set())

    def _accumulate_usage(self, usage: Dict[str, Any]) -> None:
        normalized = _projection._normalize_usage_payload(usage)
        self._usage_totals["llm_calls"] = _projection._saturating_nonnegative_add(
            self._usage_totals.get("llm_calls", 0),
            1,
        )
        if normalized.get("cached_tokens", 0) > 0 or normalized.get("cache_read_tokens", 0) > 0:
            self._usage_totals["cache_hit_calls"] = _projection._saturating_nonnegative_add(
                self._usage_totals.get("cache_hit_calls", 0),
                1,
            )
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            self._usage_totals[key] = _projection._saturating_nonnegative_add(
                self._usage_totals.get(key, 0),
                normalized.get(key, 0),
            )
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
        started_at: Optional[float] = None,
    ) -> None:
        resolved_started_at = _observed_epoch(started_at)
        self._tools.append(
            {
                "name": name,
                "kind": "tool",
                "status": "running",
                "arguments": arguments,
                "started_at": resolved_started_at,
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
            started_at=resolved_started_at,
            metadata={"tool": name},
            raw_event="tool_started",
        )
        self._append_event("tool_started", self._tools[-1])
        # 仅在主 Agent 层（depth==0）刷新可见进度，避免 subagent 内部工具刷屏。
        if depth <= 0:
            self.write(progress=f"正在调用工具 {name}。")

    def record_job_submitted(self, job_id: str) -> None:
        normalized = str(job_id or "").strip()
        if not _projection._JOB_ID_RE.fullmatch(normalized):
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
        finished_at: Optional[float] = None,
    ) -> None:
        resolved_finished_at = _observed_epoch(finished_at)
        target = self._match_running(name, span_id)
        if target is None:
            target = {"name": name, "kind": "tool", "started_at": None, "depth": depth}
            if span_id:
                target["span_id"] = span_id
            self._tools.append(target)
        target["status"] = "succeeded" if ok else "failed"
        target["finished_at"] = resolved_finished_at
        started_at = target.get("started_at")
        if isinstance(started_at, (int, float)):
            target["elapsed_s"] = round(
                max(0.0, resolved_finished_at - float(started_at)),
                1,
            )
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
            finished_at=resolved_finished_at,
            raw_event="tool_finished",
        )
        self._append_event("tool_finished", target)
        for job_id in _projection._extract_job_ids(summary, error):
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
            return None
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
        entry = {
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
        if kind == _projection._PROVIDER_OMISSION_KIND:
            self._append_event("span_started", entry)
            return
        is_provider_activity = kind in _projection._PROVIDER_ACTIVITY_KINDS
        retain_summary = True
        if is_provider_activity:
            self._provider_activity_total = _projection._saturating_nonnegative_add(
                self._provider_activity_total,
                1,
            )
            retained = sum(
                1 for item in self._tools if str(item.get("kind") or "") in _projection._PROVIDER_ACTIVITY_KINDS
            )
            retain_summary = retained < _projection.MAX_PROVIDER_ACTIVITY_SUMMARIES
            if not retain_summary:
                self._provider_activity_dropped = _projection._saturating_nonnegative_add(
                    self._provider_activity_dropped,
                    1,
                )
                entry["summary_retained"] = False
        if retain_summary:
            self._tools.append(entry)
            self._start_step(
                step_id=span_id,
                step_type=kind,
                title=name,
                parent_step_id=parent_span_id,
                depth=depth,
                started_at=started_at,
                raw_event="span_started",
            )
        if retain_summary or not is_provider_activity:
            self._append_event("span_started", entry)
        else:
            self._append_provider_activity_omission_event()
        progress = f"委托 {name} 处理中。" if kind == "subagent" else f"{name} 处理中。"
        if kind == "subagent":
            self.write(progress=progress)
        else:
            self._write_activity_progress(progress)

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
        if kind == _projection._PROVIDER_OMISSION_KIND:
            raw_count = (data or {}).get("omitted_count")
            omitted_count = (
                raw_count
                if isinstance(raw_count, int)
                and not isinstance(raw_count, bool)
                and 0 < raw_count <= (1 << 63) - 1
                else 0
            )
            self._provider_activity_total = _projection._saturating_nonnegative_add(
                self._provider_activity_total,
                omitted_count,
            )
            self._provider_activity_dropped = _projection._saturating_nonnegative_add(
                self._provider_activity_dropped,
                omitted_count,
            )
            self._append_event(
                "span_finished",
                {
                    "name": name,
                    "kind": kind,
                    "ok": ok,
                    "summary": summary,
                    "span_id": span_id,
                    "depth": depth,
                    "finished_at": finished_at,
                    "data": dict(data or {}),
                },
            )
            if omitted_count:
                self._append_provider_activity_omission_event()
            return
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
        persisted_summary = (
            ("委托执行完成。" if ok else "委托执行失败。")
            if kind == "subagent" and self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
            else summary
        )
        target = self._match_running(name, span_id)
        if target is None:
            is_provider_activity = kind in _projection._PROVIDER_ACTIVITY_KINDS
            retained = sum(
                1 for item in self._tools if str(item.get("kind") or "") in _projection._PROVIDER_ACTIVITY_KINDS
            )
            if is_provider_activity and retained >= _projection.MAX_PROVIDER_ACTIVITY_SUMMARIES:
                self._append_provider_activity_omission_event()
                self._write_activity_progress(f"{name} 完成。")
                return
            target = {"name": name, "kind": kind, "started_at": None, "depth": depth}
            if span_id:
                target["span_id"] = span_id
            self._tools.append(target)
            if is_provider_activity:
                self._provider_activity_total = _projection._saturating_nonnegative_add(
                    self._provider_activity_total,
                    1,
                )
        target["status"] = "succeeded" if ok else "failed"
        target["finished_at"] = finished_at
        started_at = target.get("started_at")
        if isinstance(started_at, (int, float)):
            target["elapsed_s"] = round(finished_at - float(started_at), 1)
        target["summary"] = persisted_summary or ""
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
            summary=persisted_summary,
            finished_at=finished_at,
            raw_event="span_finished",
        )
        transcript = (data or {}).get("transcript") if data else None
        if transcript and span_id:
            transcript_path = self._persist_subagent_transcript(span_id, name, data or {})
            if transcript_path is not None:
                target["transcript_path"] = str(transcript_path)
        self._append_event("span_finished", target)
        progress = f"委托 {name} 完成。" if kind == "subagent" else f"{name} 完成。"
        if kind == "subagent":
            self.write(progress=progress)
        else:
            self._write_activity_progress(progress)

    def context_snapshot(
        self,
        *,
        snapshot_id: str,
        runtime_id: str,
        model: str,
        iteration: int,
        session_messages: List[Dict[str, Any]],
        effective_messages: List[Dict[str, Any]],
        tool_schemas: List[Dict[str, Any]],
        resources: List[Dict[str, Any]],
        coverage: str,
        omitted: List[str],
        context_kind: str = "",
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        depth: int = 0,
        estimated_tokens: int = 0,
        model_selection: Optional[Dict[str, Any]] = None,
        private_reasoning_omission_count: int = 0,
        resource_path_omission_count: int = 0,
    ) -> None:
        """Persist one redacted model-boundary snapshot outside task.json."""

        if not _projection._CONTEXT_ID_RE.fullmatch(snapshot_id):
            raise ValueError("invalid context snapshot id")
        captured_at = time.time()
        group_turn_redacted = self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
        persisted_session_messages = [] if group_turn_redacted else session_messages
        persisted_effective_messages = [] if group_turn_redacted else effective_messages
        safe_session = omit_private_reasoning_messages(persisted_session_messages)
        safe_effective = omit_private_reasoning_messages(persisted_effective_messages)
        path_safe_session = omit_local_resource_paths(safe_session.messages)
        path_safe_effective = omit_local_resource_paths(safe_effective.messages)
        helper_results = (
            safe_session,
            safe_effective,
            path_safe_session,
            path_safe_effective,
        )
        helper_truncated = any(result.truncated for result in helper_results)
        truncation_reasons = {
            reason for result in helper_results for reason in result.truncation_reasons
        }
        reasoning_omission_count = (
            max(0, int(private_reasoning_omission_count))
            + safe_session.omission_count
            + safe_effective.omission_count
        )
        resource_path_omission_count = (
            max(0, int(resource_path_omission_count))
            + path_safe_session.omission_count
            + path_safe_effective.omission_count
        )
        effective_omitted = list(omitted)
        if (
            group_turn_redacted
            and "group_turn_text_and_transport_identity" not in effective_omitted
        ):
            effective_omitted.append("group_turn_text_and_transport_identity")
        if reasoning_omission_count and "provider_private_reasoning" not in effective_omitted:
            effective_omitted.append("provider_private_reasoning")
        if resource_path_omission_count and "local_resource_paths" not in effective_omitted:
            effective_omitted.append("local_resource_paths")
        if helper_truncated and "observability_budget_exhausted" not in effective_omitted:
            effective_omitted.append("observability_budget_exhausted")
        effective_coverage = coverage
        if coverage == "exact_model_input" and any(
            item
            in {
                "binary_resource_payload_not_persisted",
                "group_turn_text_and_transport_identity",
                "local_resource_paths",
                "observability_budget_exhausted",
                "provider_private_reasoning",
            }
            for item in effective_omitted
        ):
            effective_coverage = "partial"
        raw_payload: Dict[str, Any] = {
            "schema_version": 1,
            "task_id": self.task_id,
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "runtime_id": runtime_id,
            "model": model,
            "iteration": iteration,
            "coverage": effective_coverage,
            "omitted": effective_omitted,
            "context_kind": context_kind,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "depth": depth,
            "estimated_tokens": max(0, int(estimated_tokens)),
            "model_selection": dict(model_selection or {}),
            "session_messages": list(path_safe_session.messages),
            "effective_messages": list(path_safe_effective.messages),
            "tool_schemas": list(tool_schemas),
            "resources": list(resources),
        }
        prepared_payload = _projection._redact_group_turn_content(
            raw_payload,
            self.workspace,
            user_text=self.user_text,
            message_id=self.message_id,
        )
        redaction = redact_observability_payload(
            prepared_payload,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(self._observability_root),
        )
        truncation_reasons.update(redaction.truncation_reasons)
        payload_truncated = helper_truncated or redaction.truncated
        if redaction.truncated and "observability_budget_exhausted" not in effective_omitted:
            effective_omitted.append("observability_budget_exhausted")
            effective_coverage = "partial"
        identity_safe_value = _projection._redact_workspace_identity(
            redaction.value,
            self.workspace,
        )
        safe_payload = (
            dict(identity_safe_value)
            if isinstance(identity_safe_value, dict)
            else {
                "schema_version": 1,
                "task_id": self.task_id,
                "snapshot_id": snapshot_id,
                "captured_at": captured_at,
                "runtime_id": runtime_id,
                "model": model,
                "iteration": iteration,
            }
        )
        safe_payload["coverage"] = effective_coverage
        safe_payload["omitted"] = effective_omitted
        canonical = _projection._json_bytes(safe_payload)
        content_sha256 = hashlib.sha256(canonical).hexdigest()
        original_bytes = len(canonical)
        was_redacted = (
            redaction.replacement_count > 0
            or reasoning_omission_count > 0
            or resource_path_omission_count > 0
            or payload_truncated
            or group_turn_redacted
        )
        safe_payload.update(
            {
                "capture_status": "captured",
                "truncated": payload_truncated,
                "original_bytes": original_bytes,
                "content_sha256": content_sha256,
                "sanitization": {
                    "redacted_before_persistence": True,
                    "redacted": was_redacted,
                    "replacement_count": redaction.replacement_count,
                    "group_turn_redacted": group_turn_redacted,
                    "private_reasoning_omission_count": reasoning_omission_count,
                    "resource_path_omission_count": resource_path_omission_count,
                    "payload_truncated": payload_truncated,
                    "truncation_reasons": sorted(truncation_reasons),
                },
            }
        )
        if payload_truncated or len(_projection._json_bytes(safe_payload)) > _projection.MAX_CONTEXT_ARTIFACT_BYTES:
            safe_payload = _projection._truncated_context_payload(
                safe_payload,
                original_bytes=original_bytes,
                content_sha256=content_sha256,
            )

        selection = safe_payload.get("model_selection")
        reasoning_effort = (
            str(selection.get("reasoning_effort") or "") if isinstance(selection, dict) else ""
        )
        summary = {
            "snapshot_id": snapshot_id,
            "runtime_id": runtime_id,
            "model": model,
            "iteration": iteration,
            "coverage": effective_coverage,
            "capture_status": str(safe_payload.get("capture_status") or "captured"),
            "redacted": was_redacted,
            "truncated": bool(safe_payload.get("truncated")),
            "captured_at": captured_at,
            "message_count": len(session_messages),
            "effective_message_count": len(effective_messages),
            "tool_schema_count": len(tool_schemas),
            "resource_count": len(resources),
            "estimated_tokens": max(0, int(estimated_tokens)),
            "reasoning_effort": reasoning_effort,
            "context_kind": context_kind,
            "omitted": effective_omitted,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "depth": max(0, int(depth)),
            "role": "main" if depth <= 0 else "subagent",
        }
        try:
            _storage.write_task_artifact(
                self._path.parent, collection="contexts", name=f"{snapshot_id}.json", payload=safe_payload,
            )
        except (OSError, ValueError):
            failure_omitted = list(effective_omitted)
            if "persistence_failed" not in failure_omitted:
                failure_omitted.append("persistence_failed")
            unavailable = {
                **summary,
                "capture_status": "unavailable",
                "truncated": False,
                "omitted": failure_omitted,
            }
            self._remember_context_summary(unavailable, best_effort=True)
            return

        self._remember_context_summary(summary)

    def _remember_context_summary(
        self,
        summary: Dict[str, Any],
        *,
        best_effort: bool = False,
    ) -> None:
        snapshot_id = str(summary.get("snapshot_id") or "")
        for index, existing in enumerate(self._context_snapshots):
            if str(existing.get("snapshot_id") or "") == snapshot_id:
                self._context_snapshots[index] = summary
                break
        else:
            self._context_snapshots.append(summary)
        if not best_effort:
            self._append_event("context_snapshot", summary)
            self.write()
            return
        try:
            self._append_event("context_snapshot", summary)
        except OSError:
            pass
        try:
            self.write()
        except OSError:
            pass

    def _ensure_context_snapshot_reference(
        self,
        *,
        snapshot_id: str,
        runtime_id: str,
        model: str,
        iteration: int,
        trace_id: Optional[str],
        span_id: Optional[str],
        parent_span_id: Optional[str],
        depth: int,
        input_message_count: int,
        input_estimated_tokens: int,
        tool_schema_count: int,
        context_kind: str,
    ) -> str:
        valid_id = snapshot_id if _projection._CONTEXT_ID_RE.fullmatch(snapshot_id) else ""
        if valid_id and any(
            str(item.get("snapshot_id") or "") == valid_id for item in self._context_snapshots
        ):
            return valid_id
        if not valid_id:
            identity = ":".join(
                (
                    self.task_id,
                    str(span_id or ""),
                    str(iteration),
                    model,
                )
            )
            valid_id = f"ctx_missing_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
        reason = "snapshot_event_missing" if snapshot_id else "snapshot_id_missing"
        summary = {
            "snapshot_id": valid_id,
            "runtime_id": runtime_id or "unknown",
            "model": model,
            "iteration": max(0, int(iteration)),
            "coverage": "provider_opaque",
            "capture_status": "unavailable",
            "redacted": False,
            "truncated": False,
            "captured_at": time.time(),
            "message_count": max(0, int(input_message_count)),
            "effective_message_count": max(0, int(input_message_count)),
            "tool_schema_count": max(0, int(tool_schema_count)),
            "resource_count": 0,
            "estimated_tokens": max(0, int(input_estimated_tokens)),
            "reasoning_effort": "",
            "context_kind": context_kind,
            "omitted": [reason],
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "depth": max(0, int(depth)),
            "role": "main" if depth <= 0 else "subagent",
        }
        self._remember_context_summary(summary, best_effort=True)
        return valid_id

    def input_resources_dispatched(
        self,
        *,
        runtime_id: str,
        turn_index: int,
        request_id: str,
        resources: List[Dict[str, Any]],
    ) -> None:
        receipt = {
            "runtime_id": runtime_id,
            "turn_index": turn_index,
            "request_id": request_id,
            "resources": list(resources),
            "recorded_at": time.time(),
        }
        self._input_resources.append(receipt)
        self._append_event("input_resources_dispatched", receipt)
        self.write()

    def llm_call_started(
        self,
        *,
        model: str,
        iteration: int,
        runtime_id: str = "",
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
        context_snapshot_id: str = "",
    ) -> None:
        context_snapshot_id = self._ensure_context_snapshot_reference(
            snapshot_id=context_snapshot_id,
            runtime_id=runtime_id,
            model=model,
            iteration=iteration,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            depth=depth,
            input_message_count=input_message_count,
            input_estimated_tokens=input_estimated_tokens,
            tool_schema_count=tool_schema_count,
            context_kind=context_kind,
        )
        history = _storage.load_task_history(self.history_root)
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
            "runtime_id": runtime_id,
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
            "context_snapshot_id": context_snapshot_id,
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
                "context_snapshot_id": context_snapshot_id,
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
        runtime_id: str = "",
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
        context_snapshot_id: str = "",
        ok: bool = True,
    ) -> None:
        normalized = _projection._normalize_usage_payload(usage or {})
        effective_span_id = span_id
        existing_step = self._find_step(span_id)
        if not context_snapshot_id and existing_step is not None:
            metadata = existing_step.get("metadata")
            if isinstance(metadata, dict):
                context_snapshot_id = str(metadata.get("context_snapshot_id") or "")
        if existing_step is not None and existing_step.get("status") != "running":
            effective_span_id = f"{span_id}:{iteration}" if span_id else None
        call = {
            "model": model,
            "runtime_id": runtime_id,
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
            "context_snapshot_id": context_snapshot_id,
            "ok": ok,
        }
        step = self._find_step(effective_span_id)
        if step is None:
            self.llm_call_started(
                model=model,
                iteration=iteration,
                runtime_id=runtime_id,
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
                context_snapshot_id=context_snapshot_id,
            )
            step = self._find_step(effective_span_id)
        self._llm_calls.append(call)
        self._append_event("llm_call_finished", call)
        if step is not None:
            self._finish_step(
                step,
                ok=ok,
                summary=finish_reason or "模型调用完成",
                error=None if ok else finish_reason or "模型调用失败",
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
            normalized = _projection._normalize_usage_payload(usage)
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

    def persona_decision(
        self,
        *,
        operation: str,
        confidence: str,
        scope: str,
        reason: str,
        source: str,
        model: str = "",
        usage: Optional[Dict[str, Any]] = None,
        error_code: str = "",
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ) -> None:
        """Record the trusted host's semantic persona routing decision."""

        ended = finished_at if finished_at is not None else time.time()
        started = started_at if started_at is not None else ended
        ok = not bool(error_code)
        step = self._start_step(
            step_id=f"persona_{uuid.uuid4().hex[:12]}",
            step_type="persona_control",
            title="人格意图判定",
            parent_step_id=None,
            depth=0,
            started_at=started,
            metadata={
                "operation": operation,
                "confidence": confidence,
                "scope": scope,
                "source": source,
                "model": model,
                "error_code": error_code,
            },
            raw_event="persona_decision",
        )
        self._finish_step(
            step,
            ok=ok,
            summary=reason,
            error=error_code or None,
            finished_at=ended,
            actual_usage=usage,
            raw_event="persona_decision",
        )
        if usage:
            normalized = _projection._normalize_usage_payload(usage)
            self._llm_calls.append(
                {
                    "kind": "persona_control",
                    "model": model,
                    "iteration": -1,
                    "finish_reason": "decision" if ok else "failed",
                    "usage": normalized,
                    "role": "helper",
                    "recorded_at": ended,
                    "context_kind": "persona_control",
                    "span_id": step["step_id"],
                }
            )
            self._accumulate_usage(normalized)
        self._append_event(
            "persona_decision",
            {
                "operation": operation,
                "confidence": confidence,
                "scope": scope,
                "reason": reason,
                "source": source,
                "model": model,
                "usage": usage or {},
                "error_code": error_code,
                "started_at": started,
                "finished_at": ended,
                "step_id": step["step_id"],
            },
        )
        self.write(
            progress=(
                f"人格意图判定：{operation} / {confidence}。"
                if ok
                else f"人格意图判定失败：{error_code}。"
            )
        )

    def persona_draft(self, *, result: PersonaDraftResult) -> None:
        """Record the complete persona-draft Agent run and its real calls."""

        ended = time.time()
        started = ended - max(0, result.elapsed_ms) / 1000.0
        step = self._start_step(
            step_id=f"persona_draft_{uuid.uuid4().hex[:12]}",
            step_type="persona_control",
            title="人格草案生成",
            parent_step_id=None,
            depth=0,
            started_at=started,
            metadata={
                "model": result.model,
                "model_calls": len(result.calls),
                "search_calls": result.search_calls,
                "source_count": len(result.source_urls),
                "observed_source_count": len(result.observed_source_urls),
                "error_code": result.error_code,
                "error_kind": result.error_kind,
            },
            raw_event="persona_draft",
        )
        for call in result.calls:
            normalized = _projection._normalize_usage_payload(dict(call.usage or {}))
            call_summary = {
                "kind": "persona_draft",
                "model": call.model,
                "iteration": call.iteration,
                "finish_reason": call.finish_reason or call.error_code,
                "usage": normalized,
                "role": "helper",
                "recorded_at": ended,
                "context_kind": "persona_draft",
                "span_id": step["step_id"],
                "ok": call.ok,
                "elapsed_ms": call.elapsed_ms,
                "error_code": call.error_code,
                "error_kind": call.error_kind,
            }
            self._llm_calls.append(call_summary)
            self._accumulate_usage(normalized)
        payload = {
            "ok": result.ok,
            "model": result.model,
            "model_calls": len(result.calls),
            "search_calls": result.search_calls,
            "source_urls": list(result.source_urls),
            "source_count": len(result.source_urls),
            "observed_source_count": len(result.observed_source_urls),
            "elapsed_ms": result.elapsed_ms,
            "error_code": result.error_code,
            "error_kind": result.error_kind,
            "markdown_sha256": (
                hashlib.sha256(result.markdown.encode("utf-8")).hexdigest()
                if result.markdown
                else ""
            ),
            "step_id": step["step_id"],
        }
        self._finish_step(
            step,
            ok=result.ok,
            summary="人格草案已生成" if result.ok else "人格草案生成失败",
            error=result.error_code or None,
            finished_at=ended,
            actual_usage=dict(result.usage),
            raw_event="persona_draft",
        )
        self._append_event("persona_draft", payload)
        self.write(
            progress=(
                "人格草案已生成。"
                if result.ok
                else f"人格草案生成失败：{result.error_code or 'unknown'}。"
            )
        )

    def _persist_subagent_transcript(
        self, span_id: str, name: str, data: Dict[str, Any]
    ) -> Optional[Path]:
        try:
            artifact_stem = (
                span_id
                if _projection._ARTIFACT_SEGMENT_RE.fullmatch(span_id)
                else f"span_{hashlib.sha256(span_id.encode('utf-8')).hexdigest()[:24]}"
            )
            raw_transcript = data.get("transcript")
            transcript_items = (
                list(raw_transcript) if isinstance(raw_transcript, (list, tuple)) else []
            )
            transcript_messages = [
                item if isinstance(item, dict) else {"role": "unknown", "content": item}
                for item in transcript_items
            ]
            group_messages_omitted = self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
            persisted_messages = [] if group_messages_omitted else transcript_messages
            raw_result = data.get("result")
            persisted_result = raw_result
            if group_messages_omitted:
                result_mapping = raw_result if isinstance(raw_result, dict) else {}
                raw_outputs = result_mapping.get("outputs")
                persisted_result = {
                    "ok": bool(result_mapping.get("ok")),
                    "output_count": (
                        len(raw_outputs) if isinstance(raw_outputs, (list, tuple)) else 0
                    ),
                    "content_omitted": raw_result is not None,
                }
            reasoning_omission = omit_private_reasoning_messages(persisted_messages)
            resource_omission = omit_local_resource_paths(reasoning_omission.messages)
            payload = self._sanitize_for_persistence(
                {
                    "name": name,
                    "span_id": span_id,
                    "stop_reason": (None if group_messages_omitted else data.get("stop_reason")),
                    "result": persisted_result,
                    "transcript": list(resource_omission.messages),
                    "transcript_message_count": len(transcript_messages),
                    "omitted": (
                        [
                            "group_message_content",
                            "group_subagent_result_content",
                            "group_subagent_stop_reason",
                        ]
                        if group_messages_omitted
                        else []
                    ),
                    "sanitization": {
                        "group_message_content_omitted": group_messages_omitted,
                        "private_reasoning_omission_count": reasoning_omission.omission_count,
                        "resource_path_omission_count": resource_omission.omission_count,
                        "invalid_transcript_omitted": raw_transcript is not None
                        and not isinstance(raw_transcript, (list, tuple)),
                    },
                }
            )
            return _storage.write_task_artifact(
                self._path.parent, collection="subagents", name=f"{artifact_stem}.json", payload=payload,
            )
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
        try:
            with _storage._task_completion_lock(self._path.parent, create=True):
                self._apply_write_state(
                    status=status,
                    progress=progress,
                    finished_at=None if status == "delegated" else turn_finished_at,
                )
                self._merge_persisted_completion_state()
                known_results = {
                    str(item.get("job_id") or ""): item
                    for item in self._job_results
                    if isinstance(item, dict) and item.get("job_id")
                }
                all_jobs_complete = bool(self._job_ids) and all(
                    job_id in known_results for job_id in self._job_ids
                )
                if self._job_ids and all_jobs_complete:
                    child_results = [known_results[job_id] for job_id in self._job_ids]
                    children_succeeded = all(bool(item.get("ok")) for item in child_results)
                    # The main turn may close after every child has already
                    # persisted its result.  In that ordering the recorder is
                    # the single owner of the terminal transition.
                    self._status = (
                        "failed" if status == "failed" or not children_succeeded else "succeeded"
                    )
                    self._progress = _projection._delegated_progress(
                        child_results,
                        len(self._job_ids),
                    )
                    self._finished_at = turn_finished_at
                elif self._job_ids:
                    # Any turn with unfinished background children remains
                    # delegated so the Console keeps polling.  A main-turn
                    # failure is persisted separately in turn.json and becomes
                    # authoritative when the final child closes the task.
                    self._status = "delegated"
                    self._progress = _projection._delegated_progress(
                        list(known_results.values()),
                        len(self._job_ids),
                    )
                    self._finished_at = None
                effective_status = self._status
                effective_finished_at = (
                    None
                    if effective_status == "delegated"
                    else self._finished_at or turn_finished_at
                )
                effective_error = error
                if all_jobs_complete and effective_status == "failed" and not effective_error:
                    effective_error = "\n".join(
                        str(known_results[job_id].get("error") or "")
                        for job_id in self._job_ids
                        if not known_results[job_id].get("ok")
                    ).strip()
                effective_resources = list(produced_resources or [])
                if all_jobs_complete:
                    for job_id in self._job_ids:
                        for output in known_results[job_id].get("outputs") or []:
                            if isinstance(output, str) and output not in effective_resources:
                                effective_resources.append(output)
                turn = {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "message_id": self.message_id,
                    "user_text": self._persisted_user_text(),
                    "final_text": final_text,
                    "stop_reason": stop_reason,
                    "error": effective_error,
                    "produced_resources": effective_resources,
                    "job_results": list(self._job_results),
                    "status": effective_status,
                    "turn_finished_at": turn_finished_at,
                    "finished_at": effective_finished_at,
                }
                if status == "failed":
                    turn["main_status"] = "failed"
                if lifecycle:
                    turn.update(lifecycle)
                safe_turn = self._sanitize_for_persistence(
                    turn,
                    retain_group_current_text=True,
                )
                _storage._write_private_task_json(
                    self._path.parent,
                    _projection.TURN_FILENAME,
                    safe_turn,
                )
                self._write_task_summary_locked()
                if effective_status == "delegated":
                    self._append_event("task_delegated", turn)
                else:
                    self._append_event("task_finished", turn)
        finally:
            if self._log_context_token is not None:
                from chatcopilot.core.log_context import pop_log_context

                pop_log_context(self._log_context_token)
                self._log_context_token = None

    def record_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        self._append_event(event_type, payload)

    def set_persona_outcome(self, *, outcome: str, error_code: str = "") -> None:
        """Persist the structured persona terminal alongside task summary state."""

        self._persona_outcome = {
            "outcome": str(outcome or "")[:80],
            "error_code": str(error_code or "")[:120],
        }
        self._append_event("persona_outcome", dict(self._persona_outcome))
        self.write()

    def _append_provider_activity_omission_event(self) -> None:
        if self._provider_omission_event_written:
            return
        self._provider_omission_event_written = True
        self._append_event(
            "provider_activity_omitted",
            {
                "reason": "provider_activity_limit",
                "retained_summary_limit": _projection.MAX_PROVIDER_ACTIVITY_SUMMARIES,
                "retained_raw_event_limit": _projection.MAX_PROVIDER_ACTIVITY_RAW_EVENTS,
            },
        )

    def _append_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self._event_lock:
            _storage._append_task_event(
                self._path.parent,
                event_type,
                self._sanitize_for_persistence(
                    payload,
                    retain_group_current_text=event_type == "task_started",
                ),
                workspace_root=self._observability_root,
            )

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
        shared_group = self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
        if self.unauthenticated_intake and shared_group:
            submitter = "未验证来源"
        elif self.workspace.user_id and (self.redact_identity or shared_group):
            submitter = stable_actor_ref(
                "qq",
                self.workspace.user_id,
                conversation_id=(
                    f"{self.workspace.chat_kind or ''}:{self.workspace.chat_id or ''}"
                ),
            )
        elif shared_group:
            submitter = "未验证来源"
        else:
            submitter = self.workspace.user_name or self.workspace.user_id or ""
        return {
            "schema_version": _projection.TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "description": _projection.describe_user_text(self.user_text),
            "progress": self._progress,
            "status": self._status,
            "submitter": submitter,
            "asked_at": self.asked_at,
            "started_at": self.asked_at,
            "finished_at": finished_at,
            "turn_finished_at": self._turn_finished_at,
            "elapsed_s": elapsed_s,
            "updated_at": updated_at,
            "tools": _projection._task_tool_summaries(self._tools),
            "llm_calls": self._llm_calls,
            "context_snapshots": self._context_snapshots,
            "input_resources": self._input_resources,
            "steps": _projection._task_step_summaries(self._steps),
            "activity_summary": {
                "provider_total": self._provider_activity_total,
                "provider_retained": max(
                    0,
                    self._provider_activity_total - self._provider_activity_dropped,
                ),
                "provider_dropped": self._provider_activity_dropped,
                "truncated": self._provider_activity_dropped > 0,
            },
            "summary_limits": {
                "tools_total": len(self._tools),
                "tools_retained": min(len(self._tools), _projection.MAX_TASK_TOOL_SUMMARIES),
                "steps_total": len(self._steps),
                "steps_retained": min(len(self._steps), _projection.MAX_TASK_STEP_SUMMARIES),
                "truncated": (
                    len(self._tools) > _projection.MAX_TASK_TOOL_SUMMARIES
                    or len(self._steps) > _projection.MAX_TASK_STEP_SUMMARIES
                ),
            },
            "current_step": current.get("title") if current else self._progress,
            "usage_totals": dict(self._usage_totals),
            "forecast": dict(self._forecast),
            "primary_model": self._primary_model,
            "context_kind": self._context_kind,
            "persona_outcome": dict(self._persona_outcome),
            "job_ids": self._job_ids,
            "job_results": self._job_results,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "workspace": _projection._workspace_payload(
                self.workspace,
                redact_identity=self.redact_identity,
                unauthenticated_intake=self.unauthenticated_intake,
            ),
            "path": str(self._path.parent),
            "trace_id": self.task_id,
            "events_path": str(self._path.parent / _projection.EVENTS_FILENAME),
            "turn_path": str(self._path.parent / _projection.TURN_FILENAME),
        }


def complete_delegated_task(
    workspace: Workspace,
    *,
    task_id: str,
    job_id: str,
    result: Dict[str, Any],
    history_root: Path | None = None,
) -> Dict[str, Any] | None:
    """Merge one child result and terminalize a delegated parent when all children finish."""

    if not str(task_id or "").startswith("task_") or "/" in task_id or "\\" in task_id:
        return None
    try:
        storage_root = _storage.group_task_actor_root(workspace, create=False)
        observability_root = _storage._resolve_task_observability_root(workspace, history_root)
    except ValueError:
        return None
    task_dir = storage_root / _projection.TASKS_DIRNAME / task_id
    try:
        with _storage._task_completion_lock(task_dir):
            completion = _merge_delegated_task_completion(
                workspace,
                task_dir=task_dir,
                job_id=job_id,
                result=result,
                observability_root=observability_root,
            )
            if completion is None:
                return None
            task, completed_result, all_complete = completion
            if completed_result is None:
                return task
            try:
                _storage._append_task_event(
                    task_dir,
                    "job_completed",
                    _projection._redact_workspace_identity(
                        {"job_id": job_id, "result": completed_result},
                        workspace,
                    ),
                    workspace_root=observability_root,
                )
                if all_complete:
                    _storage._append_task_event(
                        task_dir,
                        "task_finished",
                        _projection._redact_workspace_identity(
                            {
                                "status": task["status"],
                                "job_results": task["job_results"],
                                "finished_at": task["finished_at"],
                            },
                            workspace,
                        ),
                        workspace_root=observability_root,
                    )
            except (OSError, OverflowError, ValueError) as exc:
                # task.json and turn.json are authoritative.  A bounded
                # observability sidecar failure must not turn a persisted child
                # completion into an apparent lifecycle failure.
                _LOGGER.warning(
                    "delegated task completion event append failed | task=%s error=%s",
                    task_id,
                    type(exc).__name__,
                )
            return task
    except OSError:
        return None


def _merge_delegated_task_completion(
    workspace: Workspace,
    *,
    task_dir: Path,
    job_id: str,
    result: Dict[str, Any],
    observability_root: Path,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], bool] | None:
    task = _storage._read_private_task_json(task_dir, _projection.TASK_FILENAME)
    if not isinstance(task, dict):
        return None
    turn = _storage._read_private_task_json(task_dir, _projection.TURN_FILENAME) or {}
    if not isinstance(turn, dict):
        turn = {}
    main_failed = str(turn.get("main_status") or "") == "failed" or (
        str(turn.get("status") or "") == "failed"
        and isinstance(turn.get("turn_finished_at"), (int, float))
        and not isinstance(turn.get("turn_finished_at"), bool)
        and math.isfinite(float(turn["turn_finished_at"]))
    )

    summaries = {
        str(item.get("job_id") or ""): dict(item)
        for item in task.get("job_results") or []
        if isinstance(item, dict) and item.get("job_id")
    }
    result_already_recorded = job_id in summaries
    already_terminal = result_already_recorded and task.get("status") in {"succeeded", "failed"}
    if not result_already_recorded:
        summaries[job_id] = _projection._job_result_summary(
            job_id,
            result,
            omit_free_text=workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED,
        )
    ordered_ids = [str(item) for item in task.get("job_ids") or [] if str(item)]
    if job_id not in ordered_ids:
        ordered_ids.append(job_id)
    ordered_results = [summaries[item] for item in ordered_ids if item in summaries]

    now = time.time()
    children_complete = bool(ordered_ids) and len(ordered_results) == len(ordered_ids)
    turn_finished_at = task.get("turn_finished_at")
    turn_finished = (
        isinstance(turn_finished_at, (int, float))
        and not isinstance(turn_finished_at, bool)
        and math.isfinite(float(turn_finished_at))
    )
    # A fast child may finish before ToolFinished has registered every job the
    # still-running main turn will submit.  Until the main turn's registration
    # boundary is durably closed, merge the receipt but never claim terminal.
    all_complete = already_terminal or (turn_finished and children_complete)
    if not already_terminal:
        task["job_ids"] = ordered_ids
        task["job_results"] = ordered_results
        task["updated_at"] = now
        task["progress"] = _projection._delegated_progress(ordered_results, len(ordered_ids))
        if all_complete:
            succeeded = not main_failed and all(bool(item.get("ok")) for item in ordered_results)
            task["status"] = "succeeded" if succeeded else "failed"
            task["finished_at"] = now
            started_at = task.get("started_at") or task.get("asked_at")
            if isinstance(started_at, (int, float)):
                task["elapsed_s"] = round(now - float(started_at), 1)
        elif turn_finished:
            task["status"] = "delegated"
            task["finished_at"] = None
            task["elapsed_s"] = None
        task_redaction = redact_observability_payload(
            task,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(observability_root),
        )
        _storage._write_private_task_json(
            task_dir,
            _projection.TASK_FILENAME,
            _projection._redact_workspace_identity(task_redaction.value, workspace),
        )

    if isinstance(turn, dict):
        turn["job_results"] = ordered_results
        turn["status"] = task["status"]
        turn["finished_at"] = task.get("finished_at") if all_complete else None
        if all_complete:
            turn["produced_resources"] = [
                output
                for item in ordered_results
                for output in item.get("outputs") or []
                if isinstance(output, str)
            ]
            if not all(bool(item.get("ok")) for item in ordered_results):
                errors = [str(turn.get("error") or "").strip()]
                errors.extend(
                    str(item.get("error") or "").strip()
                    for item in ordered_results
                    if not item.get("ok")
                )
                turn["error"] = "\n".join(dict.fromkeys(error for error in errors if error))
        turn_redaction = redact_observability_payload(
            turn,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(observability_root),
        )
        _storage._write_private_task_json(
            task_dir,
            _projection.TURN_FILENAME,
            _projection._redact_workspace_identity(turn_redaction.value, workspace),
        )
    return (
        task,
        None if result_already_recorded else summaries[job_id],
        all_complete,
    )


def _observed_epoch(value: Optional[float]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return time.time()
    normalized = float(value)
    return normalized if normalized >= 0.0 and math.isfinite(normalized) else time.time()


__all__ = [
    "MAX_PROVIDER_ACTIVITY_RAW_EVENTS",
    "TASK_SCHEMA_VERSION",
    "TASK_FILENAME",
    "TASKS_DIRNAME",
    "TurnTaskRecorder",
    "complete_delegated_task",
    "describe_user_text",
    "group_task_actor_root",
    "group_task_intake_root",
    "make_task_id",
]
