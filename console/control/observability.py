"""Read-only Console job, task, and log observability services."""
from __future__ import annotations

import json
import math
import os
import re
import select
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from console.control import systemd
from console.control.instances import BotInstance
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets,
    default_observability_roots,
    load_bounded_observability_json,
    redact_observability_payload,
)


_CONSOLE_UNIT = "chatcopilot-console.service"
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_.-]+$")
_JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9_.-]+$")
_CONTEXT_SNAPSHOT_ID_RE = re.compile(r"^ctx_[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_MAX_CONTEXT_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_JOB_JSON_BYTES = 8 * 1024 * 1024
_DEFAULT_TASK_EVENT_LIMIT = 500
_MAX_TASK_EVENT_LIMIT = 1000
_MAX_EVENT_FILE_TAIL_BYTES = 512 * 1024
_MAX_SAFE_COUNT = (1 << 63) - 1


class UnsafeContextSnapshotError(RuntimeError):
    """A context artifact exists but cannot be served through the Console."""


def _open_path_without_symlink_ancestors(path: Path, flags: int) -> int:
    """Open a file through directory FDs so no path component can be a link."""

    absolute = path.absolute()
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(absolute.anchor or os.sep, directory_flags)
    try:
        for part in absolute.parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(absolute.name, flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _read_job_json(path: Path) -> Dict[str, object]:
    """读取 job 目录里的 JSON 附属文件；缺失或损坏时静默返回空 dict。"""
    try:
        expected = path.lstat()
        if (
            stat.S_ISLNK(expected.st_mode)
            or not stat.S_ISREG(expected.st_mode)
            or expected.st_nlink != 1
            or bool(stat.S_IMODE(expected.st_mode) & 0o022)
            or expected.st_size > _MAX_JOB_JSON_BYTES
            or (os.name == "posix" and expected.st_uid != os.geteuid())
        ):
            return {}
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = _open_path_without_symlink_ancestors(path, flags)
        try:
            current = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or bool(stat.S_IMODE(current.st_mode) & 0o022)
                or current.st_size > _MAX_JOB_JSON_BYTES
                or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
                or (os.name == "posix" and current.st_uid != os.geteuid())
            ):
                return {}
            chunks: list[bytes] = []
            remaining = current.st_size + 1
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _MAX_JOB_JSON_BYTES:
                return {}
        finally:
            os.close(fd)
        loaded = load_bounded_observability_json(
            raw,
            max_bytes=_MAX_JOB_JSON_BYTES,
        )
        if not loaded.ok:
            return {}
        data = loaded.value
    except OSError:
        return {}
    if not isinstance(data, dict):
        return {}
    redaction = redact_observability_payload(
        data,
        secrets=collect_observability_secrets(),
        roots=default_observability_roots(path.parent.parent.parent),
    )
    if not isinstance(redaction.value, dict):
        return {}
    safe = dict(redaction.value)
    if redaction.truncated:
        safe["payload_truncated"] = True
        sanitization = (
            dict(safe.get("sanitization"))
            if isinstance(safe.get("sanitization"), dict)
            else {}
        )
        sanitization.update(
            {
                "payload_truncated": True,
                "truncation_reasons": list(redaction.truncation_reasons),
            }
        )
        safe["sanitization"] = sanitization
    return safe


def _coerce_epoch(value: object) -> Optional[float]:
    try:
        epoch = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(epoch) or epoch <= 0:
        return None
    return epoch


def _coerce_non_negative_int(value: object) -> int:
    try:
        normalized = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    if normalized < 0 or normalized > _MAX_SAFE_COUNT:
        return 0
    return normalized


def _coerce_non_negative_float(value: object) -> float | None:
    try:
        normalized = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(normalized) or normalized < 0:
        return None
    return normalized


def _job_dir_mtime(job_dir: Path) -> Optional[float]:
    try:
        return job_dir.stat().st_mtime
    except OSError:
        return None


def _read_stdout_tail(
    path: Path,
    *,
    max_lines: int = 20,
    max_chars: int = 4000,
) -> tuple[str, Optional[float], bool]:
    """Read a bounded private log tail without following replacement links."""

    try:
        expected = path.lstat()
    except FileNotFoundError:
        return "", None, False
    except OSError:
        return "", None, True
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISREG(expected.st_mode)
        or expected.st_nlink != 1
        or bool(stat.S_IMODE(expected.st_mode) & 0o022)
        or (os.name == "posix" and expected.st_uid != os.geteuid())
    ):
        return "", None, True
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = _open_path_without_symlink_ancestors(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or bool(stat.S_IMODE(opened.st_mode) & 0o022)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            or (os.name == "posix" and opened.st_uid != os.geteuid())
        ):
            return "", None, True
        start = max(0, opened.st_size - 32768)
        os.lseek(descriptor, start, os.SEEK_SET)
        raw_chunk = os.read(descriptor, 32768)
        modified_at = opened.st_mtime
    except OSError:
        return "", None, True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if start:
        _, separator, raw_chunk = raw_chunk.partition(b"\n")
        if not separator:
            return "", modified_at, False
    chunk = raw_chunk.decode("utf-8", errors="replace")
    tail = "\n".join(chunk.splitlines()[-max_lines:]).strip()
    redaction = redact_observability_payload(
        tail,
        secrets=collect_observability_secrets(),
        roots=default_observability_roots(path.parent.parent.parent),
    )
    tail = redaction.value if isinstance(redaction.value, str) else ""
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail, modified_at, False


def jobs(inst: BotInstance, *, limit: int = 50) -> Dict[str, object]:
    ws = Path(inst.workspace_root) if inst.workspace_root else None
    out: List[Dict[str, object]] = []
    workspace_exists = bool(ws and ws.is_dir())
    if ws and ws.is_dir():
        # 深度无关：私聊 p2p_<user>/jobs/ 在 1 层，群聊 group_<chat>/user_<user>/jobs/
        # 在 2 层；用 ** 统一覆盖，避免群聊任务被漏扫。
        for status_file in ws.glob("**/jobs/*/status.json"):
            data = _read_job_json(status_file)
            job_dir = status_file.parent
            user_dir = job_dir.parent.parent.name
            request = _read_job_json(job_dir / "request.json")
            result = _read_job_json(job_dir / "result.json")
            workspace = request.get("workspace") if isinstance(request.get("workspace"), dict) else {}
            submitter = (
                workspace.get("user_name")
                or workspace.get("user_id")
                or user_dir
            )
            submitted_at = _coerce_epoch(request.get("submitted_at"))
            updated_at = _coerce_epoch(data.get("updated_at"))
            started_at = _coerce_epoch(result.get("started_at"))
            finished_at = _coerce_epoch(result.get("finished_at"))
            elapsed_s = None
            status_value = str(data.get("status") or "unknown")
            if started_at is not None and finished_at is not None:
                elapsed_s = round(finished_at - started_at, 1)
            elif started_at is not None and status_value == "running":
                elapsed_s = round(time.time() - started_at, 1)
            stdout_log = job_dir / "stdout.log"
            progress_tail, stdout_modified_at, stdout_integrity_gap = _read_stdout_tail(
                stdout_log
            )
            stdout_age = (
                max(0, int(time.time() - stdout_modified_at))
                if stdout_modified_at is not None
                else None
            )
            sort_time = (
                updated_at
                or finished_at
                or started_at
                or submitted_at
                or _job_dir_mtime(job_dir)
                or 0
            )
            out.append(
                {
                    "job_id": job_dir.name,
                    "user_dir": user_dir,
                    "tool_name": request.get("tool_name", ""),
                    "submitter": submitter,
                    "status": status_value,
                    "message": str(data.get("message") or ""),
                    "stage": str(data.get("stage") or status_value),
                    "error_code": str(data.get("error_code") or result.get("error_code") or ""),
                    "details": (
                        data.get("details")
                        if isinstance(data.get("details"), dict)
                        else result.get("details") if isinstance(result.get("details"), dict) else {}
                    ),
                    "submitted_at": submitted_at,
                    "updated_at": updated_at,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "elapsed_s": elapsed_s,
                    "sort_time": sort_time,
                    "progress_tail": progress_tail,
                    "stdout_age_s": stdout_age,
                    "progress_tail_integrity_gap": stdout_integrity_gap,
                    "path": str(job_dir),
                }
            )
    out.sort(key=lambda j: float(j.get("sort_time") or 0), reverse=True)
    visible_jobs = out[:limit]
    return {
        "instance_id": inst.instance_id,
        "workspace_root": str(ws) if ws else "",
        "workspace_exists": workspace_exists,
        "count": len(out),
        "integrity_gap": any(
            bool(job.get("progress_tail_integrity_gap")) for job in visible_jobs
        ),
        "jobs": visible_jobs,
    }


def _task_summary(data: Dict[str, object], task_dir: Path) -> Dict[str, object]:
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    asked_at = _coerce_epoch(data.get("asked_at"))
    updated_at = _coerce_epoch(data.get("updated_at"))
    finished_at = _coerce_epoch(data.get("finished_at"))
    started_at = _coerce_epoch(data.get("started_at")) or asked_at
    sort_time = updated_at or finished_at or asked_at or _job_dir_mtime(task_dir) or 0
    return {
        "schema_version": 2,
        "task_id": str(data.get("task_id") or task_dir.name),
        "description": str(data.get("description") or ""),
        "progress": str(data.get("progress") or ""),
        "current_step": str(data.get("current_step") or data.get("progress") or ""),
        "status": str(data.get("status") or "unknown"),
        "submitter": (
            data.get("submitter")
            or workspace.get("user_name")
            or workspace.get("user_id")
            or task_dir.parent.parent.name
        ),
        "asked_at": asked_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_s": data.get("elapsed_s"),
        "updated_at": updated_at,
        "sort_time": sort_time,
        "usage_totals": data.get("usage_totals") if isinstance(data.get("usage_totals"), dict) else {},
        "forecast": data.get("forecast") if isinstance(data.get("forecast"), dict) else {},
        "primary_model": str(data.get("primary_model") or ""),
        "context_kind": str(data.get("context_kind") or ""),
        "context_snapshots": _context_snapshot_summaries(data),
        "activity_summary": _activity_summary(data),
        "summary_limits": _summary_limits(data),
        "job_ids": [
            str(item)
            for item in data.get("job_ids") or []
            if isinstance(item, str) and _JOB_ID_RE.fullmatch(item)
        ],
    }


def _activity_summary(data: Dict[str, object]) -> Dict[str, object]:
    raw = data.get("activity_summary")
    source = raw if isinstance(raw, dict) else {}
    return {
        "provider_total": _coerce_non_negative_int(source.get("provider_total")),
        "provider_retained": _coerce_non_negative_int(source.get("provider_retained")),
        "provider_dropped": _coerce_non_negative_int(source.get("provider_dropped")),
        "truncated": bool(source.get("truncated", False)),
    }


def _summary_limits(data: Dict[str, object]) -> Dict[str, object]:
    raw = data.get("summary_limits")
    source = raw if isinstance(raw, dict) else {}
    return {
        "tools_total": _coerce_non_negative_int(source.get("tools_total")),
        "tools_retained": _coerce_non_negative_int(source.get("tools_retained")),
        "steps_total": _coerce_non_negative_int(source.get("steps_total")),
        "steps_retained": _coerce_non_negative_int(source.get("steps_retained")),
        "context_snapshots_total": _coerce_non_negative_int(
            source.get("context_snapshots_total")
        ),
        "context_snapshots_retained": _coerce_non_negative_int(
            source.get("context_snapshots_retained")
        ),
        "context_snapshots_truncated": bool(
            source.get("context_snapshots_truncated", False)
        ),
        "context_snapshots_minimal": bool(
            source.get("context_snapshots_minimal", False)
        ),
        "llm_calls_total": _coerce_non_negative_int(source.get("llm_calls_total")),
        "llm_calls_retained": _coerce_non_negative_int(
            source.get("llm_calls_retained")
        ),
        "llm_calls_truncated": bool(source.get("llm_calls_truncated", False)),
        "input_resources_total": _coerce_non_negative_int(
            source.get("input_resources_total")
        ),
        "input_resources_retained": _coerce_non_negative_int(
            source.get("input_resources_retained")
        ),
        "input_resources_truncated": bool(
            source.get("input_resources_truncated", False)
        ),
        "payload_truncated": bool(source.get("payload_truncated", False)),
        "truncated": bool(source.get("truncated", False)),
    }


def _context_snapshot_summaries(data: Dict[str, object]) -> List[Dict[str, object]]:
    """Project only the bounded metadata needed by the polled task views."""
    raw_snapshots = data.get("context_snapshots")
    if not isinstance(raw_snapshots, list):
        return []
    snapshots: List[Dict[str, object]] = []
    for raw in raw_snapshots:
        if not isinstance(raw, dict):
            continue
        snapshot_id = str(raw.get("snapshot_id") or "")
        if not _CONTEXT_SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            continue
        omitted = raw.get("omitted") if isinstance(raw.get("omitted"), list) else []
        snapshots.append(
            {
                "snapshot_id": snapshot_id,
                "backend": str(raw.get("backend") or ""),
                "model": str(raw.get("model") or ""),
                "iteration": _coerce_non_negative_int(raw.get("iteration")),
                "coverage": str(raw.get("coverage") or "provider_opaque"),
                "capture_status": str(raw.get("capture_status") or "captured"),
                "redacted": bool(raw.get("redacted", True)),
                "truncated": bool(raw.get("truncated", False)),
                "captured_at": _coerce_epoch(raw.get("captured_at")),
                "message_count": _coerce_non_negative_int(raw.get("message_count")),
                "effective_message_count": _coerce_non_negative_int(
                    raw.get("effective_message_count")
                ),
                "tool_schema_count": _coerce_non_negative_int(raw.get("tool_schema_count")),
                "resource_count": _coerce_non_negative_int(raw.get("resource_count")),
                "estimated_tokens": _coerce_non_negative_int(raw.get("estimated_tokens")),
                "reasoning_effort": str(raw.get("reasoning_effort") or ""),
                "context_kind": str(raw.get("context_kind") or ""),
                "omitted": [str(item) for item in omitted if isinstance(item, str)],
                "trace_id": str(raw.get("trace_id") or ""),
                "span_id": str(raw.get("span_id") or ""),
                "parent_span_id": str(raw.get("parent_span_id") or ""),
                "depth": _coerce_non_negative_int(raw.get("depth")),
                "role": (
                    "subagent" if str(raw.get("role") or "") == "subagent" else "main"
                ),
            }
        )
    return snapshots


def tasks(inst: BotInstance, *, limit: int = 50) -> Dict[str, object]:
    ws = Path(inst.workspace_root) if inst.workspace_root else None
    out: List[Dict[str, object]] = []
    workspace_exists = bool(ws and ws.is_dir())
    safe_limit = min(50, max(1, int(limit)))
    if ws and ws.is_dir():
        for task_file in ws.glob("**/tasks/*/task.json"):
            data = _read_job_json(task_file)
            if data.get("schema_version") != 2:
                continue
            out.append(_task_summary(data, task_file.parent))
    out.sort(key=lambda item: float(item.get("sort_time") or 0), reverse=True)
    return {
        "instance_id": inst.instance_id,
        "workspace_root": str(ws) if ws else "",
        "workspace_exists": workspace_exists,
        "count": min(len(out), safe_limit),
        "tasks": out[:safe_limit],
    }


def _resolve_task(inst: BotInstance, task_id: str) -> tuple[Path, Dict[str, object]] | None:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid task id")
    ws = Path(inst.workspace_root).resolve() if inst.workspace_root else None
    if ws is None or not ws.is_dir():
        return None
    for task_file in ws.glob(f"**/tasks/{task_id}/task.json"):
        try:
            resolved = task_file.resolve()
            resolved.relative_to(ws)
        except (OSError, ValueError):
            continue
        data = _read_job_json(resolved)
        if data.get("schema_version") == 2 and str(data.get("task_id") or task_id) == task_id:
            return resolved.parent, data
    return None


def _read_json_lines(path: Path) -> List[Dict[str, object]]:
    events, _truncated, _integrity_gap = _read_json_lines_tail(
        path,
        limit=_MAX_TASK_EVENT_LIMIT,
    )
    secrets = collect_observability_secrets()
    roots = default_observability_roots(path.parent.parent.parent)
    safe_events: List[Dict[str, object]] = []
    for event in events:
        redaction = redact_observability_payload(event, secrets=secrets, roots=roots)
        if isinstance(redaction.value, dict):
            safe_events.append(redaction.value)
    return safe_events


def _read_json_lines_tail(
    path: Path,
    *,
    limit: int,
) -> tuple[List[Dict[str, object]], bool, bool]:
    """Read a bounded tail from one append-only JSONL file.

    Keep the containing task/job directory open while resolving the file.  A
    final-component ``O_NOFOLLOW`` alone is insufficient because an attacker
    can replace an ancestor with a symlink between validation and ``open``.
    """

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor = _open_path_without_symlink_ancestors(
            path.parent,
            directory_flags,
        )
        parent_stat = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or bool(stat.S_IMODE(parent_stat.st_mode) & 0o022)
            or (os.name == "posix" and parent_stat.st_uid != os.geteuid())
        ):
            os.close(parent_descriptor)
            parent_descriptor = None
            return [], True, True
        expected = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        return [], False, False
    except OSError:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        return [], True, True
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        os.close(parent_descriptor)
        return [], True, True
    if expected.st_nlink != 1:
        os.close(parent_descriptor)
        return [], True, True
    if hasattr(os, "getuid") and expected.st_uid != os.getuid():
        os.close(parent_descriptor)
        return [], True, True
    if stat.S_IMODE(expected.st_mode) & 0o022:
        os.close(parent_descriptor)
        return [], True, True
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            or opened.st_nlink != 1
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            return [], True, True
        size = opened.st_size
        start = max(0, size - _MAX_EVENT_FILE_TAIL_BYTES)
        os.lseek(descriptor, start, os.SEEK_SET)
        raw = os.read(descriptor, _MAX_EVENT_FILE_TAIL_BYTES)
    except OSError:
        return [], True, True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
    if start:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    integrity_gap = False
    if raw and not raw.endswith(b"\n"):
        integrity_gap = True
    lines = raw.splitlines()
    truncated = start > 0 or len(lines) > limit
    events: List[Dict[str, object]] = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            integrity_gap = True
            continue
        if isinstance(item, dict):
            events.append(item)
        else:
            integrity_gap = True
    previous_sequence: int | None = None
    for event in events:
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            continue
        if previous_sequence is not None and sequence != previous_sequence + 1:
            integrity_gap = True
        previous_sequence = sequence
    return events, truncated or integrity_gap, integrity_gap


def _find_job_dir(task_dir: Path, job_id: str) -> Path | None:
    if not _JOB_ID_RE.fullmatch(job_id):
        return None
    workspace_dir = task_dir.parent.parent
    candidates = [workspace_dir / "jobs" / job_id]
    protected_state = next(
        (parent for parent in task_dir.parents if parent.name == ".conversation-state"),
        None,
    )
    if protected_state is not None and workspace_dir.parent.name == "task-actors":
        candidates.insert(
            0,
            protected_state / "jobs" / workspace_dir.name / job_id,
        )
    candidate = next((path for path in candidates if path.is_dir()), candidates[0])
    try:
        expected = candidate.lstat()
        if (
            stat.S_ISLNK(expected.st_mode)
            or not stat.S_ISDIR(expected.st_mode)
            or bool(stat.S_IMODE(expected.st_mode) & 0o022)
            or (os.name == "posix" and expected.st_uid != os.geteuid())
        ):
            return None
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        descriptor = _open_path_without_symlink_ancestors(candidate, directory_flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or bool(stat.S_IMODE(opened.st_mode) & 0o022)
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
                or (os.name == "posix" and opened.st_uid != os.geteuid())
            ):
                return None
        finally:
            os.close(descriptor)
    except (FileNotFoundError, OSError):
        return None
    request = _read_job_json(candidate / "request.json")
    if str(request.get("job_id") or "") != job_id:
        return None
    return candidate


def _job_steps(task_dir: Path, data: Dict[str, object]) -> tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    steps: List[Dict[str, object]] = []
    statuses: List[Dict[str, object]] = []
    task_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    for raw_job_id in data.get("job_ids") or []:
        job_id = str(raw_job_id)
        job_dir = _find_job_dir(task_dir, job_id)
        if job_dir is None:
            continue
        status = _read_job_json(job_dir / "status.json")
        stage_events = _read_json_lines(job_dir / "status-events.jsonl")
        parent_step_id = next(
            (
                str(step.get("step_id"))
                for step in reversed(task_steps)
                if isinstance(step, dict)
                and job_id in json.dumps(step, ensure_ascii=False, default=str)
            ),
            None,
        )
        first_at = _coerce_epoch(
            (stage_events[0].get("recorded_at") if stage_events else None)
            or status.get("created_at")
        )
        final_at = _coerce_epoch(status.get("updated_at"))
        job_status = str(status.get("status") or "unknown")
        job_step_id = f"job:{job_id}"
        raw_depth = next(
            (
                step.get("depth", 0)
                for step in task_steps
                if isinstance(step, dict) and step.get("step_id") == parent_step_id
            ),
            0,
        )
        job_depth = min(100, _coerce_non_negative_int(raw_depth)) + 1
        steps.append(
            {
                "step_id": job_step_id,
                "type": "background_job",
                "parent_step_id": parent_step_id,
                "depth": job_depth,
                "status": job_status,
                "title": job_id,
                "started_at": first_at,
                "finished_at": final_at if job_status in {"succeeded", "failed", "cancelled"} else None,
                "elapsed_s": round(final_at - first_at, 4) if first_at and final_at and job_status in {"succeeded", "failed", "cancelled"} else None,
                "summary": str(status.get("message") or ""),
                "error": str(status.get("error_code") or "") or None,
                "metadata": {"job_id": job_id, "stage": str(status.get("stage") or job_status)},
                "estimated_usage": {},
                "actual_usage": {},
                "inclusive_usage": {},
                "raw_event_types": ["job_stage_changed"],
            }
        )
        for index, event in enumerate(stage_events):
            event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
            started = _coerce_epoch(event.get("recorded_at"))
            next_started = _coerce_epoch(stage_events[index + 1].get("recorded_at")) if index + 1 < len(stage_events) else None
            terminal = index + 1 == len(stage_events) and job_status in {"succeeded", "failed", "cancelled"}
            ended = next_started or (final_at if terminal else None)
            stage_status = "failed" if terminal and job_status == "failed" else ("succeeded" if ended else "running")
            steps.append(
                {
                    "step_id": f"{job_step_id}:stage:{index}",
                    "type": "job_stage",
                    "parent_step_id": job_step_id,
                    "depth": job_depth + 1,
                    "status": stage_status,
                    "title": str(event_data.get("stage") or event_data.get("status") or "unknown"),
                    "started_at": started,
                    "finished_at": ended,
                    "elapsed_s": round(ended - started, 4) if started and ended else None,
                    "summary": str(event_data.get("message") or ""),
                    "error": str(event_data.get("error_code") or "") or None,
                    "metadata": {"job_id": job_id, "stage": event_data.get("stage")},
                    "estimated_usage": {},
                    "actual_usage": {},
                    "inclusive_usage": {},
                    "raw_event_types": ["job_stage_changed"],
                }
            )
        statuses.append(
            {
                "job_id": job_id,
                "status": job_status,
                "stage": str(status.get("stage") or job_status),
                "message": str(status.get("message") or ""),
                "error_code": str(status.get("error_code") or ""),
            }
        )
    return steps, statuses


def _timing_breakdown(steps: List[Dict[str, object]]) -> Dict[str, float]:
    totals = {
        "model_s": 0.0,
        "activity_s": 0.0,
        "tool_s": 0.0,
        "background_s": 0.0,
        "routing_s": 0.0,
    }

    def accumulate(key: str, elapsed: float) -> None:
        combined = totals[key] + elapsed
        totals[key] = combined if math.isfinite(combined) else sys.float_info.max

    for step in steps:
        elapsed = _coerce_non_negative_float(step.get("elapsed_s"))
        if elapsed is None:
            continue
        step_type = str(step.get("type") or "")
        if step_type == "llm":
            accumulate("model_s", elapsed)
        elif step_type in {
            "command",
            "file_change",
            "mcp_tool",
            "plan",
            "reasoning",
            "web_search",
            "provider_event",
        }:
            accumulate("activity_s", elapsed)
        elif step_type == "tool":
            accumulate("tool_s", elapsed)
        elif step_type == "job_stage":
            accumulate("background_s", elapsed)
        elif step_type == "routing":
            accumulate("routing_s", elapsed)
    return {key: round(value, 4) for key, value in totals.items()}


def _actual_cost(llm_calls: List[object]) -> Dict[str, object]:
    prices = {
        "deepseek-v4-pro": (3.0, 0.025, 6.0),
        "deepseek-v4-flash": (1.0, 0.02, 2.0),
        "deepseek-chat": (1.0, 0.02, 2.0),
        "deepseek-reasoner": (1.0, 0.02, 2.0),
    }
    total = 0.0
    priced_calls = 0
    unpriced: set[str] = set()
    for call in llm_calls:
        if not isinstance(call, dict):
            continue
        model = str(call.get("model") or "").lower()
        usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
        price = prices.get(model)
        if price is None:
            if model:
                unpriced.add(model)
            continue
        prompt = _coerce_non_negative_int(usage.get("prompt_tokens") or 0)
        cached = min(
            prompt,
            max(
                _coerce_non_negative_int(usage.get("cached_tokens") or 0),
                _coerce_non_negative_int(usage.get("cache_read_tokens") or 0),
            ),
        )
        completion = _coerce_non_negative_int(usage.get("completion_tokens") or 0)
        total += (
            (prompt - cached) * price[0]
            + cached * price[1]
            + completion * price[2]
        ) / 1_000_000
        priced_calls += 1
    return {
        "status": "estimated" if priced_calls and not unpriced else ("partial" if priced_calls else "unpriced"),
        "estimated_rmb": round(total, 8),
        "priced_calls": priced_calls,
        "unpriced_models": sorted(unpriced),
        "note": "Local price-table estimate; not a provider invoice.",
    }


def task_detail(inst: BotInstance, task_id: str) -> Dict[str, object] | None:
    resolved = _resolve_task(inst, task_id)
    if resolved is None:
        return None
    task_dir, data = resolved
    raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    steps = [dict(step) for step in raw_steps if isinstance(step, dict)]
    job_steps, job_statuses = _job_steps(task_dir, data)
    steps.extend(job_steps)
    summary = _task_summary(data, task_dir)
    llm_calls = data.get("llm_calls") if isinstance(data.get("llm_calls"), list) else []
    return {
        **summary,
        "steps": steps,
        "timing": _timing_breakdown(steps),
        "llm_calls": llm_calls,
        "actual_usage": data.get("usage_totals") if isinstance(data.get("usage_totals"), dict) else {},
        "actual_cost": _actual_cost(llm_calls),
        "forecast": data.get("forecast") if isinstance(data.get("forecast"), dict) else {},
        "job_statuses": job_statuses,
        "session_id": str(data.get("session_id") or ""),
        "message_id": str(data.get("message_id") or ""),
    }


def task_events(
    inst: BotInstance,
    task_id: str,
    *,
    limit: int = _DEFAULT_TASK_EVENT_LIMIT,
) -> Dict[str, object] | None:
    resolved = _resolve_task(inst, task_id)
    if resolved is None:
        return None
    task_dir, data = resolved
    bounded_limit = min(_MAX_TASK_EVENT_LIMIT, max(1, int(limit)))
    events, truncated, integrity_gap = _read_json_lines_tail(
        task_dir / "events.jsonl",
        limit=bounded_limit,
    )
    last_task_time = 0.0
    for event in events:
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or sequence < 0:
            continue
        recorded_at = _coerce_epoch(event.get("recorded_at")) or last_task_time
        last_task_time = max(last_task_time, recorded_at)
        event["_merge_recorded_at"] = last_task_time
    for raw_job_id in data.get("job_ids") or []:
        job_id = str(raw_job_id)
        job_dir = _find_job_dir(task_dir, job_id)
        if job_dir is None:
            continue
        job_events, job_truncated, job_integrity_gap = _read_json_lines_tail(
            job_dir / "status-events.jsonl",
            limit=bounded_limit,
        )
        truncated = truncated or job_truncated
        integrity_gap = integrity_gap or job_integrity_gap
        for event in job_events:
            events.append({**event, "source": "job", "job_id": job_id})
    events.sort(key=_event_sort_key)
    if len(events) > bounded_limit:
        events = events[-bounded_limit:]
        truncated = True
    safe_events: List[Dict[str, object]] = []
    for event in events:
        event.pop("_merge_recorded_at", None)
        redaction = redact_observability_payload(
            event,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(task_dir.parent.parent),
        )
        if isinstance(redaction.value, dict):
            sanitization = (
                dict(redaction.value.get("sanitization"))
                if isinstance(redaction.value.get("sanitization"), dict)
                else {}
            )
            sanitization.update(
                {
                    "redacted_for_console": True,
                    "redacted": bool(sanitization.get("redacted"))
                    or redaction.replacement_count > 0,
                }
            )
            redaction.value["sanitization"] = sanitization
            safe_events.append(redaction.value)
    return {
        "task_id": task_id,
        "count": len(safe_events),
        "limit": bounded_limit,
        "truncated": truncated,
        "integrity_gap": integrity_gap,
        "events": safe_events,
    }


def _event_sort_key(item: Dict[str, object]) -> tuple[float, int, str]:
    recorded_at = (
        _coerce_epoch(item.get("_merge_recorded_at"))
        or _coerce_epoch(item.get("recorded_at"))
        or 0.0
    )
    sequence = item.get("sequence")
    safe_sequence = sequence if isinstance(sequence, int) and sequence >= 0 else 2**63 - 1
    return recorded_at, safe_sequence, str(item.get("event_id") or "")


def context_snapshot(
    inst: BotInstance,
    task_id: str,
    snapshot_id: str,
) -> Dict[str, object] | None:
    """Read one task-bound context artifact without following filesystem links."""
    if not _CONTEXT_SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise ValueError("invalid context snapshot id")
    resolved = _resolve_task(inst, task_id)
    if resolved is None:
        return None
    task_dir, task_data = resolved
    listed_snapshots = task_data.get("context_snapshots")
    if not isinstance(listed_snapshots, list) or not any(
        isinstance(item, dict) and str(item.get("snapshot_id") or "") == snapshot_id
        for item in listed_snapshots
    ):
        return None
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    task_descriptor: int | None = None
    contexts_descriptor: int | None = None
    descriptor: int | None = None
    candidate_name = f"{snapshot_id}.json"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        task_descriptor = _open_path_without_symlink_ancestors(
            task_dir,
            directory_flags,
        )
        opened_task = os.fstat(task_descriptor)
        if (
            not stat.S_ISDIR(opened_task.st_mode)
            or bool(stat.S_IMODE(opened_task.st_mode) & 0o022)
            or (os.name == "posix" and opened_task.st_uid != os.geteuid())
        ):
            raise UnsafeContextSnapshotError("context snapshot task directory is unsafe")

        contexts_lstat = os.stat(
            "contexts",
            dir_fd=task_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(contexts_lstat.st_mode) or not stat.S_ISDIR(
            contexts_lstat.st_mode
        ):
            raise UnsafeContextSnapshotError("context snapshot directory is unsafe")
        contexts_descriptor = os.open(
            "contexts",
            directory_flags,
            dir_fd=task_descriptor,
        )
        opened_contexts = os.fstat(contexts_descriptor)
        if (
            not stat.S_ISDIR(opened_contexts.st_mode)
            or (opened_contexts.st_dev, opened_contexts.st_ino)
            != (contexts_lstat.st_dev, contexts_lstat.st_ino)
            or (os.name == "posix" and opened_contexts.st_uid != os.geteuid())
        ):
            raise UnsafeContextSnapshotError("context snapshot directory is unsafe")
        if stat.S_IMODE(opened_contexts.st_mode) & 0o077:
            raise UnsafeContextSnapshotError(
                "context snapshot directory permissions are too broad"
            )

        candidate_lstat = os.stat(
            candidate_name,
            dir_fd=contexts_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(candidate_lstat.st_mode) or not stat.S_ISREG(
            candidate_lstat.st_mode
        ):
            raise UnsafeContextSnapshotError("context snapshot is not a regular file")
        descriptor = os.open(
            candidate_name,
            flags,
            dir_fd=contexts_descriptor,
        )
    except FileNotFoundError:
        for opened_descriptor in (
            descriptor,
            contexts_descriptor,
            task_descriptor,
        ):
            if opened_descriptor is not None:
                os.close(opened_descriptor)
        return None
    except UnsafeContextSnapshotError:
        for opened_descriptor in (
            descriptor,
            contexts_descriptor,
            task_descriptor,
        ):
            if opened_descriptor is not None:
                os.close(opened_descriptor)
        raise
    except OSError as exc:
        for opened_descriptor in (
            descriptor,
            contexts_descriptor,
            task_descriptor,
        ):
            if opened_descriptor is not None:
                os.close(opened_descriptor)
        raise UnsafeContextSnapshotError("context snapshot cannot be opened safely") from exc
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise UnsafeContextSnapshotError("context snapshot is not a regular file")
        if opened_stat.st_nlink != 1:
            raise UnsafeContextSnapshotError("context snapshot must have exactly one hard link")
        if hasattr(os, "getuid") and opened_stat.st_uid != os.getuid():
            raise UnsafeContextSnapshotError("context snapshot owner does not match the Console user")
        if stat.S_IMODE(opened_stat.st_mode) & 0o077:
            raise UnsafeContextSnapshotError("context snapshot permissions are too broad")
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            candidate_lstat.st_dev,
            candidate_lstat.st_ino,
        ):
            raise UnsafeContextSnapshotError("context snapshot changed while opening")
        if opened_stat.st_size > _MAX_CONTEXT_SNAPSHOT_BYTES:
            raise UnsafeContextSnapshotError("context snapshot exceeds the 8 MiB limit")
        chunks: List[bytes] = []
        remaining = _MAX_CONTEXT_SNAPSHOT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if contexts_descriptor is not None:
            os.close(contexts_descriptor)
        if task_descriptor is not None:
            os.close(task_descriptor)
    if len(payload) > _MAX_CONTEXT_SNAPSHOT_BYTES:
        raise UnsafeContextSnapshotError("context snapshot exceeds the 8 MiB limit")
    loaded = load_bounded_observability_json(
        payload,
        max_bytes=_MAX_CONTEXT_SNAPSHOT_BYTES,
    )
    if not loaded.ok:
        reason = (
            "context snapshot is not valid UTF-8 JSON or exceeds the parsing budget"
            if loaded.budget_exhausted
            else "context snapshot is not valid UTF-8 JSON"
        )
        raise UnsafeContextSnapshotError(reason)
    decoded = loaded.value
    if not isinstance(decoded, dict):
        raise UnsafeContextSnapshotError("context snapshot root must be an object")
    artifact_snapshot_id = str(decoded.get("snapshot_id") or "")
    if artifact_snapshot_id != snapshot_id:
        raise UnsafeContextSnapshotError("context snapshot identity does not match its path")
    artifact_task_id = str(decoded.get("task_id") or "")
    if artifact_task_id != task_id:
        raise UnsafeContextSnapshotError("context snapshot is bound to another task")
    sanitization = decoded.get("sanitization")
    if not isinstance(sanitization, dict) or sanitization.get(
        "redacted_before_persistence"
    ) is not True:
        raise UnsafeContextSnapshotError(
            "context snapshot lacks redaction provenance"
        )
    redaction = redact_observability_payload(
        decoded,
        secrets=collect_observability_secrets(),
        roots=default_observability_roots(task_dir.parent.parent),
    )
    if not isinstance(redaction.value, dict):
        raise UnsafeContextSnapshotError("context snapshot could not be normalized safely")
    safe_sanitization = (
        dict(redaction.value.get("sanitization"))
        if isinstance(redaction.value.get("sanitization"), dict)
        else {}
    )
    safe_sanitization.update(
        {
            "redacted_for_console": True,
            "redacted": bool(safe_sanitization.get("redacted"))
            or redaction.replacement_count > 0,
            "console_replacement_count": redaction.replacement_count,
            "payload_truncated": bool(
                safe_sanitization.get("payload_truncated")
            )
            or redaction.truncated,
            "console_truncation_reasons": list(redaction.truncation_reasons),
        }
    )
    if redaction.truncated:
        redaction.value["truncated"] = True
        redaction.value["coverage"] = "partial"
        omitted = (
            list(redaction.value.get("omitted"))
            if isinstance(redaction.value.get("omitted"), list)
            else []
        )
        if "console_observability_budget_exhausted" not in omitted:
            omitted.append("console_observability_budget_exhausted")
        redaction.value["omitted"] = omitted
    redaction.value["sanitization"] = safe_sanitization
    return redaction.value


# ---------------------------------------------------------------------------
# 日志：tail + follow（供 SSE）
# ---------------------------------------------------------------------------
def resolve_log_files(inst: BotInstance, source: str = "cc") -> List[str]:
    if source == "questions":
        f = inst.questions_log_file()
    elif source == "runtime":
        f = inst.runtime_log_file()
    else:
        f = inst.cc_log_file()
    return [f] if f else []


def tail_log(path: str, lines: int = 200) -> List[str]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        content = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return content[-lines:]


# SSE keepalive 哨兵：空闲轮询时 yield，让上层能发心跳并感知客户端断开。
KEEPALIVE = "\x00"


def follow_log(path: str, *, from_end_lines: int = 200, poll_interval: float = 1.0) -> Iterator[str]:
    """先吐尾部 N 行，再持续 poll 增量；空闲时 yield KEEPALIVE 哨兵。"""
    for line in tail_log(path, from_end_lines):
        yield line
    pos = 0
    p = Path(path)
    if p.is_file():
        pos = p.stat().st_size
    while True:
        if not p.is_file():
            yield KEEPALIVE
            time.sleep(poll_interval)
            continue
        size = p.stat().st_size
        if size < pos:
            pos = 0  # 文件被轮转/截断
        if size > pos:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            for line in chunk.splitlines():
                yield line
        else:
            yield KEEPALIVE
        time.sleep(poll_interval)


def console_log_error() -> Optional[str]:
    """返回控制台 journald 日志不可读的原因；可读时返回 None。"""
    if not systemd.is_available():
        return "systemd --user 不可用，无法读取控制台服务日志"
    try:
        cp = subprocess.run(
            ["journalctl", "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"journalctl 不可用：{exc}"
    if cp.returncode != 0:
        return (cp.stderr or cp.stdout or "journalctl 检查失败").strip()
    return None


def follow_console_log(*, from_end_lines: int = 200) -> Iterator[str]:
    """跟随控制台后端 systemd journal；生成器关闭时终止 journalctl。"""
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    args = [
        "journalctl",
        "--user",
        "-u",
        _CONSOLE_UNIT,
        "--no-pager",
        "--output=short-iso",
        f"--lines={max(0, from_end_lines)}",
        "-f",
    ]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        yield f"[ERR] 无法启动 journalctl：{exc}"
        return

    try:
        assert proc.stdout is not None
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line == "":
                    break
                yield line.rstrip("\n")
            elif proc.poll() is not None:
                break
            else:
                yield KEEPALIVE
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

__all__ = [
    "KEEPALIVE",
    "console_log_error",
    "follow_console_log",
    "follow_log",
    "jobs",
    "resolve_log_files",
    "tail_log",
    "task_detail",
    "task_events",
    "context_snapshot",
    "tasks",
    "UnsafeContextSnapshotError",
]
