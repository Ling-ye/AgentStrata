"""Platform-neutral background job status helpers."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from chatcopilot.contracts.code_tasks import (
    CODE_TASK_RESUMABLE_STATUSES,
    CODE_TASK_TERMINAL_STATUSES,
    CODE_TASK_TOOL,
    validate_code_task_transition,
)
from chatcopilot.core.workspace import Workspace
from chatcopilot.project import ENV_PREFIX, LIMIT_DIRNAME

JOBS_DIRNAME = "jobs"
NOTIFICATION_FILENAME = "notification.json"
RESULT_FILENAME = "result.json"
REQUEST_FILENAME = "request.json"
STATUS_FILENAME = "status.json"
STATUS_EVENTS_FILENAME = "status-events.jsonl"
COMPLETED_STATUSES = set(CODE_TASK_TERMINAL_STATUSES)
_QUEUES_DIRNAME = "queues"


@dataclass(frozen=True)
class BackgroundJob:
    job_id: str
    tool_name: str
    execution_policy: str
    job_dir: Path
    request_path: Path
    result_path: Path
    session_id: str | None = None
    user_id: str | None = None
    queue_name: str = ""
    queue_position: int | None = None

    @property
    def status_path(self) -> Path:
        return self.job_dir / STATUS_FILENAME

    @property
    def cancellation_path(self) -> Path:
        return self.job_dir / "cancel-request.json"


def safe_segment(value: object) -> str:
    text = str(value or "").strip()
    safe = "".join(ch if (ch.isalnum() or ch in "-_.@") else "_" for ch in text)
    return safe.strip("_") or "default"


def queue_root() -> Path:
    raw = os.environ.get(f"{ENV_PREFIX}_LIMIT_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(tempfile.gettempdir())
    return base / LIMIT_DIRNAME / _QUEUES_DIRNAME


def queue_position(queue_name: str, job_id: str) -> int | None:
    queue_dir = queue_root() / safe_segment(queue_name)
    if not queue_dir.is_dir():
        return None
    entries = sorted(queue_dir.glob("*.queue"), key=lambda p: p.name)
    for idx, entry in enumerate(entries, start=1):
        if job_id in entry.name:
            return idx
    return None


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


@contextmanager
def code_task_state_lock(job_dir: Path) -> Iterator[None]:
    """Serialize cancellation against the non-cancellable delivery transition."""
    if os.name != "posix":
        raise RuntimeError("isolated code tasks require a POSIX runtime")
    import fcntl

    lock_path = job_dir / ".code-task-state.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_job_status(
    job_dir: Path,
    status: str,
    message: str,
    *,
    stage: str = "",
    error_code: str = "",
    details: dict[str, Any] | None = None,
    heartbeat_at: float | None = None,
    resource: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = read_json_file(job_dir / STATUS_FILENAME) or {}
    request = read_json_file(job_dir / REQUEST_FILENAME) or {}
    if str(request.get("tool_name") or "") == CODE_TASK_TOOL:
        validate_code_task_transition(
            str(previous.get("status") or ""),
            status,
        )
    now = time.time()
    payload = {
        "status": status,
        "message": message,
        "stage": stage or status,
        "error_code": error_code,
        "details": dict(details or {}),
        "attempt": int(previous.get("attempt") or 1),
        "created_at": float(previous.get("created_at") or now),
        "updated_at": now,
        "heartbeat_at": heartbeat_at
        if heartbeat_at is not None
        else previous.get("heartbeat_at"),
        "resource": dict(
            resource
            if resource is not None
            else (
                previous.get("resource")
                if isinstance(previous.get("resource"), dict)
                else {}
            )
        ),
    }
    write_json_atomic(job_dir / STATUS_FILENAME, payload)
    previous_key = (
        str(previous.get("status") or ""),
        str(previous.get("stage") or previous.get("status") or ""),
    )
    current_key = (payload["status"], payload["stage"])
    if previous_key != current_key:
        event = {
            "event": "job_stage_changed",
            "recorded_at": now,
            "data": {
                "previous_status": previous_key[0],
                "previous_stage": previous_key[1],
                **payload,
            },
        }
        events_path = job_dir / STATUS_EVENTS_FILENAME
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return payload


def read_job_result(job: BackgroundJob) -> dict[str, Any] | None:
    return read_json_file(job.result_path)


def read_job_status(job: BackgroundJob) -> dict[str, Any] | None:
    return read_json_file(job.job_dir / STATUS_FILENAME)


def read_job_notification(job: BackgroundJob) -> dict[str, Any] | None:
    return read_json_file(job.job_dir / NOTIFICATION_FILENAME)


def find_job(workspace: Workspace, job_id: str) -> BackgroundJob | None:
    safe_job_id = safe_segment(job_id)
    if safe_job_id != job_id:
        return None
    job_dir = workspace.root / JOBS_DIRNAME / safe_job_id
    if not job_dir.is_dir():
        return None
    return _job_from_dir(job_dir, workspace)


def is_job_completed(job: BackgroundJob) -> bool:
    status = read_job_status(job) or {}
    state = str(status.get("status") or "")
    if state:
        return state in COMPLETED_STATUSES
    return job.result_path.is_file()


def latest_code_job(
    workspace: Workspace,
    *,
    user_id: str | None = None,
) -> BackgroundJob | None:
    jobs_root = workspace.root / JOBS_DIRNAME
    if not jobs_root.is_dir():
        return None
    dirs = sorted(
        (path for path in jobs_root.iterdir() if path.is_dir() and path.name.startswith("job_")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for job_dir in dirs:
        job = _job_from_dir(job_dir, workspace)
        if job is None or job.tool_name != CODE_TASK_TOOL:
            continue
        if user_id and job.user_id and job.user_id != user_id:
            continue
        return job
    return None


def request_job_cancel(job: BackgroundJob, *, requested_by: str) -> bool:
    state_lock = (
        code_task_state_lock(job.job_dir)
        if job.tool_name == CODE_TASK_TOOL
        else nullcontext()
    )
    with state_lock:
        status = read_job_status(job) or {}
        state = str(status.get("status") or "")
        if state in COMPLETED_STATUSES:
            return False
        if job.tool_name == CODE_TASK_TOOL and state == "delivering":
            return False
        write_json_atomic(
            job.cancellation_path,
            {
                "requested_at": time.time(),
                "requested_by": requested_by,
            },
        )
        try:
            write_job_status(
                job.job_dir,
                "cancel_requested",
                "Cancellation requested.",
                stage=str(status.get("stage") or "cancel_requested"),
                details={
                    **(
                        status.get("details")
                        if isinstance(status.get("details"), dict)
                        else {}
                    ),
                    "cancel_requested": True,
                },
            )
        except Exception:
            job.cancellation_path.unlink(missing_ok=True)
            raise
        return True


def append_code_task_attempt(
    job: BackgroundJob,
    *,
    prompt: str,
    title: str,
    delivery_only: bool,
    requested_by: str,
) -> int:
    request = read_json_file(job.request_path) or {}
    if str(request.get("tool_name") or "") != CODE_TASK_TOOL:
        raise ValueError("job is not a code task")
    status = read_job_status(job) or {}
    state = str(status.get("status") or "")
    if state not in CODE_TASK_RESUMABLE_STATUSES:
        raise RuntimeError(f"code task is not resumable from status: {state or 'unknown'}")
    attempts = request.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    number = len(attempts) + 1
    attempts.append(
        {
            "number": number,
            "prompt": prompt,
            "title": title,
            "delivery_only": delivery_only,
            "submitted_at": time.time(),
            "requested_by": requested_by,
            "status": "queued",
        }
    )
    request["attempts"] = attempts
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    request["args"] = {
        **args,
        "prompt": prompt,
        "title": title,
        "delivery_only": delivery_only,
    }
    write_json_atomic(job.request_path, request)
    if job.result_path.is_file():
        attempts_dir = job.job_dir / "attempt-results"
        attempts_dir.mkdir(parents=True, exist_ok=True)
        job.result_path.replace(attempts_dir / f"attempt-{number - 1}.json")
    job.cancellation_path.unlink(missing_ok=True)
    payload = write_job_status(
        job.job_dir,
        "queued",
        "Resume attempt queued.",
        stage="queued",
        details={"resumed": True},
    )
    payload["attempt"] = number
    write_json_atomic(job.status_path, payload)
    return number


def list_unnotified_completed_jobs(
    workspace: Workspace,
    *,
    session_id: str | None = None,
    limit: int = 20,
) -> list[BackgroundJob]:
    jobs_root = workspace.root / JOBS_DIRNAME
    if not jobs_root.is_dir():
        return []

    jobs: list[BackgroundJob] = []
    dirs = sorted(
        (p for p in jobs_root.iterdir() if p.is_dir() and p.name.startswith("job_")),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for job_dir in dirs:
        job = _job_from_dir(job_dir, workspace)
        if job is None:
            continue
        if session_id and job.session_id and job.session_id != session_id:
            continue
        if not is_job_completed(job):
            continue
        notification = read_job_notification(job) or {}
        if notification.get("delivery") == "delivered":
            continue
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def job_notification_workspace(
    job: BackgroundJob,
    *,
    fallback: Workspace | None = None,
) -> Workspace:
    request = read_json_file(job.request_path) or {}
    workspace_payload = request.get("workspace") if isinstance(request.get("workspace"), dict) else {}
    notify = request.get("notify") if isinstance(request.get("notify"), dict) else {}
    root = (
        str(workspace_payload.get("root") or "").strip()
        or (str(fallback.root) if fallback is not None else "")
        or str(job.job_dir.parent.parent)
    )
    return Workspace(
        root=Path(root).expanduser().resolve(),
        chat_kind=str(notify.get("chat_kind") or workspace_payload.get("chat_kind") or "").strip() or None,
        chat_id=str(notify.get("chat_id") or workspace_payload.get("chat_id") or "").strip() or None,
        user_id=str(notify.get("user_id") or workspace_payload.get("user_id") or job.user_id or "").strip() or None,
        user_name=str(notify.get("user_name") or workspace_payload.get("user_name") or "").strip() or None,
    )


def _job_from_dir(job_dir: Path, workspace: Workspace) -> BackgroundJob | None:
    request = read_json_file(job_dir / REQUEST_FILENAME) or {}
    job_id = str(request.get("job_id") or job_dir.name)
    if safe_segment(job_id) != job_id:
        return None
    notify = request.get("notify") if isinstance(request.get("notify"), dict) else {}
    queue_name = str(request.get("queue_name") or "")
    return BackgroundJob(
        job_id=job_id,
        tool_name=str(request.get("tool_name") or ""),
        execution_policy=str(request.get("execution_policy") or ""),
        job_dir=job_dir,
        request_path=job_dir / REQUEST_FILENAME,
        result_path=job_dir / RESULT_FILENAME,
        session_id=str(notify.get("session_id") or "") or None,
        user_id=str(notify.get("user_id") or workspace.user_id or "") or None,
        queue_name=queue_name,
        queue_position=queue_position(queue_name, job_id),
    )


__all__ = [
    "BackgroundJob",
    "COMPLETED_STATUSES",
    "JOBS_DIRNAME",
    "NOTIFICATION_FILENAME",
    "REQUEST_FILENAME",
    "RESULT_FILENAME",
    "STATUS_FILENAME",
    "STATUS_EVENTS_FILENAME",
    "append_code_task_attempt",
    "code_task_state_lock",
    "find_job",
    "is_job_completed",
    "job_notification_workspace",
    "latest_code_job",
    "list_unnotified_completed_jobs",
    "queue_position",
    "queue_root",
    "read_job_notification",
    "read_job_result",
    "read_job_status",
    "read_json_file",
    "request_job_cancel",
    "safe_segment",
    "write_job_status",
    "write_json_atomic",
]
