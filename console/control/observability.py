"""Read-only Console job, task, and log observability services."""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from console.control import systemd
from console.control.instances import BotInstance


_CONSOLE_UNIT = "chatcopilot-console.service"
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_.-]+$")
_JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9_.-]+$")

def _read_job_json(path: Path) -> Dict[str, object]:
    """读取 job 目录里的 JSON 附属文件；缺失或损坏时静默返回空 dict。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_epoch(value: object) -> Optional[float]:
    try:
        epoch = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return epoch


def _job_dir_mtime(job_dir: Path) -> Optional[float]:
    try:
        return job_dir.stat().st_mtime
    except OSError:
        return None


def _read_stdout_tail(path: Path, *, max_lines: int = 20, max_chars: int = 4000) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - 32768), os.SEEK_SET)
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    tail = "\n".join(chunk.splitlines()[-max_lines:]).strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


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
            stdout_age = None
            if stdout_log.is_file():
                stdout_age = int(time.time() - stdout_log.stat().st_mtime)
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
                    "progress_tail": _read_stdout_tail(stdout_log),
                    "stdout_age_s": stdout_age,
                    "path": str(job_dir),
                }
            )
    out.sort(key=lambda j: float(j.get("sort_time") or 0), reverse=True)
    return {
        "instance_id": inst.instance_id,
        "workspace_root": str(ws) if ws else "",
        "workspace_exists": workspace_exists,
        "count": len(out),
        "jobs": out[:limit],
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
        "tools": data.get("tools") if isinstance(data.get("tools"), list) else [],
        "llm_calls": data.get("llm_calls") if isinstance(data.get("llm_calls"), list) else [],
        "forecast": data.get("forecast") if isinstance(data.get("forecast"), dict) else {},
        "primary_model": str(data.get("primary_model") or ""),
        "context_kind": str(data.get("context_kind") or ""),
        "job_ids": [
            str(item)
            for item in data.get("job_ids") or []
            if isinstance(item, str) and _JOB_ID_RE.fullmatch(item)
        ],
    }


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
    if not path.is_file():
        return []
    events: List[Dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
    except OSError:
        return []
    return events


def _find_job_dir(task_dir: Path, job_id: str) -> Path | None:
    if not _JOB_ID_RE.fullmatch(job_id):
        return None
    workspace_dir = task_dir.parent.parent.resolve()
    candidate = (workspace_dir / "jobs" / job_id).resolve()
    try:
        candidate.relative_to(workspace_dir)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


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
        job_depth = int(next((step.get("depth", 0) for step in task_steps if isinstance(step, dict) and step.get("step_id") == parent_step_id), 0)) + 1
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
    totals = {"model_s": 0.0, "tool_s": 0.0, "background_s": 0.0, "routing_s": 0.0}
    for step in steps:
        elapsed = step.get("elapsed_s")
        if not isinstance(elapsed, (int, float)):
            continue
        step_type = str(step.get("type") or "")
        if step_type == "llm":
            totals["model_s"] += float(elapsed)
        elif step_type == "tool":
            totals["tool_s"] += float(elapsed)
        elif step_type == "job_stage":
            totals["background_s"] += float(elapsed)
        elif step_type == "routing":
            totals["routing_s"] += float(elapsed)
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
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        cached = min(
            prompt,
            max(
                int(usage.get("cached_tokens") or 0),
                int(usage.get("cache_read_tokens") or 0),
            ),
        )
        completion = max(0, int(usage.get("completion_tokens") or 0))
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


def task_events(inst: BotInstance, task_id: str) -> Dict[str, object] | None:
    resolved = _resolve_task(inst, task_id)
    if resolved is None:
        return None
    task_dir, data = resolved
    events = _read_json_lines(task_dir / "events.jsonl")
    for raw_job_id in data.get("job_ids") or []:
        job_id = str(raw_job_id)
        job_dir = _find_job_dir(task_dir, job_id)
        if job_dir is None:
            continue
        for event in _read_json_lines(job_dir / "status-events.jsonl"):
            events.append({**event, "source": "job", "job_id": job_id})
    events.sort(key=lambda item: float(item.get("recorded_at") or 0))
    return {"task_id": task_id, "count": len(events), "events": events}


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
    "tasks",
]
