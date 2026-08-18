"""Platform-neutral background job status helpers."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
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
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets,
    default_observability_roots,
    load_bounded_observability_json,
    redact_observability_payload,
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
_MAX_STATUS_EVENT_BYTES = 64 * 1024
_MAX_JOB_JSON_BYTES = 8 * 1024 * 1024
_LOGGER = logging.getLogger(__name__)


class _JobJsonTooLargeError(OSError):
    """A private job JSON artifact exceeded the bounded reader contract."""

    def __init__(self, size_bytes: int) -> None:
        super().__init__("private job JSON exceeds the hard size limit")
        self.size_bytes = max(0, int(size_bytes))


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


def job_storage_root(workspace: Workspace, *, create: bool = False) -> Path:
    """Return the authoritative job-control root for the current actor.

    Ordinary workspaces retain ``<workspace>/jobs``.  A shared-group workspace
    is member-writable data, so its Owner job control plane lives under the
    protected conversation sibling and is partitioned by the authenticated
    stable actor.  The shared workspace remains the worker's data workspace.
    """

    if workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        root = workspace.root / JOBS_DIRNAME
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root
    if workspace.root.name != "shared" or not workspace.chat_id or not workspace.user_id:
        raise ValueError("shared-group job storage requires stable chat and actor identities")
    actor_digest = hashlib.sha256(
        (
            f"{workspace.chat_kind or 'group'}\0{workspace.chat_id}\0{workspace.user_id}"
        ).encode("utf-8")
    ).hexdigest()
    state_root = workspace.root.parent / ".conversation-state"
    jobs_root = state_root / JOBS_DIRNAME
    actor_root = jobs_root / actor_digest
    if create:
        for path in (state_root, jobs_root, actor_root):
            if path.is_symlink():
                raise RuntimeError("protected group job directory must not be a symlink")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.is_dir() or path.is_symlink():
                raise RuntimeError("protected group job path must be a real directory")
            path.chmod(0o700)
            info = path.stat()
            if os.name == "posix" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise RuntimeError("protected group job directory must be owner-only")
    return actor_root


def iter_job_request_paths(workspace_root: Path) -> tuple[Path, ...]:
    """List legacy and protected actor-scoped job requests below one instance."""

    root = Path(workspace_root)
    if not root.is_dir():
        return ()
    paths = {
        path
        for pattern in (
            "**/jobs/job_*/request.json",
            "**/.conversation-state/jobs/*/job_*/request.json",
        )
        for path in root.glob(pattern)
    }
    return tuple(sorted(paths))


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
    # Job artifacts cross a process boundary.  Route every canonical
    # ``.../jobs/<job-id>/<artifact>.json`` caller through the same private
    # directory/openat validation, including older call sites that still use
    # this compatibility helper directly.
    if path.parent.parent.name == JOBS_DIRNAME and path.parent.name.startswith("job_"):
        try:
            return _read_private_job_json(path.parent, path.name)
        except OSError:
            return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_JOB_JSON_BYTES:
            return None
        with path.open("rb") as handle:
            raw = handle.read(_MAX_JOB_JSON_BYTES + 1)
        loaded = load_bounded_observability_json(
            raw,
            max_bytes=_MAX_JOB_JSON_BYTES,
        )
        if not loaded.ok:
            return None
        payload = loaded.value
    except OSError:
        return None
    return payload if isinstance(payload, dict) else None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    if path.parent.parent.name == JOBS_DIRNAME and path.parent.name.startswith("job_"):
        _write_private_job_json(path, payload)
        return
    _write_json_atomic_unchecked(path, payload)


def _write_json_atomic_unchecked(path: Path, payload: dict[str, Any]) -> None:
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


def _open_private_child_dir_at(parent_fd: int, name: str, *, create: bool) -> int:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise OSError("private job directory name is invalid")
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (os.name == "posix" and current.st_uid != os.geteuid())
        ):
            raise OSError("private job directory identity is unsafe")
        if stat.S_IMODE(current.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
            current = os.fstat(fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o700
            or (os.name == "posix" and current.st_uid != os.geteuid())
        ):
            raise OSError("private job directory could not be secured")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_directory_path_no_symlinks(path: Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_job_dir(job_dir: Path, *, create: bool) -> int | None:
    if safe_segment(job_dir.name) != job_dir.name:
        raise OSError("job observability path is invalid")
    if os.name != "posix":  # pragma: no cover - native Windows validation required
        parent = job_dir.parent
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise OSError("job parent must be a real directory")
        try:
            current = job_dir.lstat()
        except FileNotFoundError:
            if not create:
                raise
            job_dir.mkdir(mode=0o700)
            current = job_dir.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise OSError("private job directory must be a real directory")
        job_dir.chmod(0o700)
        return None

    parent = job_dir.parent
    parent_fd: int | None = None
    try:
        try:
            parent_fd = _open_directory_path_no_symlinks(parent)
        except FileNotFoundError:
            if not create:
                raise
            grandparent_fd = _open_directory_path_no_symlinks(parent.parent)
            try:
                parent_fd = _open_private_child_dir_at(
                    grandparent_fd,
                    parent.name,
                    create=True,
                )
            finally:
                os.close(grandparent_fd)
        current_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or current_parent.st_uid != os.geteuid()
        ):
            raise OSError("job parent identity is unsafe")
        return _open_private_child_dir_at(parent_fd, job_dir.name, create=create)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _read_private_job_json_at(dir_fd: int, name: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or bool(stat.S_IMODE(current.st_mode) & 0o022)
            or (os.name == "posix" and current.st_uid != os.geteuid())
        ):
            raise OSError("private job JSON is unsafe")
        if current.st_size > _MAX_JOB_JSON_BYTES:
            raise _JobJsonTooLargeError(current.st_size)
        raw = bytearray()
        while len(raw) <= _MAX_JOB_JSON_BYTES:
            chunk = os.read(fd, min(64 * 1024, _MAX_JOB_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > _MAX_JOB_JSON_BYTES:
            raise _JobJsonTooLargeError(len(raw))
        loaded = load_bounded_observability_json(raw, max_bytes=_MAX_JOB_JSON_BYTES)
        if loaded.budget_exhausted:
            raise _JobJsonTooLargeError(len(raw))
        if not loaded.ok:
            raise OSError("private job JSON is malformed")
        payload = loaded.value
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise OSError("private job JSON root must be an object")
    return payload


def _read_private_job_json_path(job_dir: Path, name: str) -> dict[str, Any] | None:
    """Native-Windows fallback with pre/post-open identity validation."""

    path = job_dir / name
    try:
        expected = path.lstat()
    except FileNotFoundError:
        return None
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISREG(expected.st_mode)
        or expected.st_nlink != 1
        or bool(stat.S_IMODE(expected.st_mode) & 0o022)
    ):
        raise OSError("private job JSON is unsafe")
    if expected.st_size > _MAX_JOB_JSON_BYTES:
        raise _JobJsonTooLargeError(expected.st_size)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or bool(stat.S_IMODE(current.st_mode) & 0o022)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise OSError("private job JSON is unsafe")
        if current.st_size > _MAX_JOB_JSON_BYTES:
            raise _JobJsonTooLargeError(current.st_size)
        raw = bytearray()
        while len(raw) <= _MAX_JOB_JSON_BYTES:
            chunk = os.read(fd, min(64 * 1024, _MAX_JOB_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > _MAX_JOB_JSON_BYTES:
            raise _JobJsonTooLargeError(len(raw))
        loaded = load_bounded_observability_json(raw, max_bytes=_MAX_JOB_JSON_BYTES)
        if loaded.budget_exhausted:
            raise _JobJsonTooLargeError(len(raw))
        if not loaded.ok:
            raise OSError("private job JSON is malformed")
        payload = loaded.value
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise OSError("private job JSON root must be an object")
    return payload


def _read_private_job_json(job_dir: Path, name: str) -> dict[str, Any] | None:
    job_dir_fd = _open_private_job_dir(job_dir, create=False)
    try:
        if job_dir_fd is None:  # pragma: no cover - native Windows validation required
            return _read_private_job_json_path(job_dir, name)
        return _read_private_job_json_at(job_dir_fd, name)
    finally:
        if job_dir_fd is not None:
            os.close(job_dir_fd)


def _read_job_artifact(job_dir: Path, name: str) -> dict[str, Any] | None:
    try:
        return _read_private_job_json(job_dir, name)
    except OSError:
        return None


def _write_private_job_json_at(
    dir_fd: int,
    name: str,
    payload: dict[str, Any],
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_JOB_JSON_BYTES:
        raise ValueError("private job JSON exceeds the hard size limit")
    temp_name = f".{name}.{os.urandom(8).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temp_exists = False
    try:
        fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        temp_exists = True
        try:
            current = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (os.name == "posix" and current.st_uid != os.geteuid())
            ):
                raise OSError("private job temporary file is unsafe")
            os.fchmod(fd, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("failed to write private job JSON")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
            or bool(stat.S_IMODE(existing.st_mode) & 0o022)
            or (os.name == "posix" and existing.st_uid != os.geteuid())
        ):
            raise OSError("existing private job JSON is unsafe")
        os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp_exists = False
    finally:
        if temp_exists:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


def _oversized_result_manifest(
    payload: dict[str, Any],
    *,
    encoded: bytes,
) -> dict[str, Any]:
    started_at = payload.get("started_at")
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(float(started_at))
    ):
        started_at = None
    finished_at = payload.get("finished_at")
    if (
        isinstance(finished_at, bool)
        or not isinstance(finished_at, (int, float))
        or not math.isfinite(float(finished_at))
    ):
        finished_at = time.time()
    return {
        "job_id": str(payload.get("job_id") or "")[:256],
        "tool_name": str(payload.get("tool_name") or "")[:256],
        "ok": False,
        "summary": "Job result artifact exceeded the 8 MiB safety limit.",
        "outputs": [],
        "error": "The complete result was omitted because it exceeded the safety limit.",
        "error_code": "result_artifact_too_large",
        "details": {
            "payload_truncated": True,
            "artifact_size_bytes": len(encoded),
            "max_bytes": _MAX_JOB_JSON_BYTES,
            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        },
        "stage": "failed",
        "started_at": started_at,
        "finished_at": finished_at,
        "payload_truncated": True,
    }


def _write_private_job_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    bounded_payload = payload
    if len(encoded) > _MAX_JOB_JSON_BYTES:
        if path.name != RESULT_FILENAME:
            raise ValueError("private job JSON exceeds the hard size limit")
        bounded_payload = _oversized_result_manifest(payload, encoded=encoded)

    job_dir_fd = _open_private_job_dir(path.parent, create=True)
    try:
        if job_dir_fd is None:  # pragma: no cover - native Windows validation required
            try:
                existing = path.lstat()
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                stat.S_ISLNK(existing.st_mode)
                or not stat.S_ISREG(existing.st_mode)
                or existing.st_nlink != 1
                or bool(stat.S_IMODE(existing.st_mode) & 0o022)
            ):
                raise OSError("existing private job JSON is unsafe")
            _write_json_atomic_unchecked(path, bounded_payload)
            path.chmod(0o600)
        else:
            _write_private_job_json_at(job_dir_fd, path.name, bounded_payload)
    finally:
        if job_dir_fd is not None:
            os.close(job_dir_fd)


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
    job_dir_fd = _open_private_job_dir(job_dir, create=True)
    try:
        if job_dir_fd is None:  # pragma: no cover - native Windows validation required
            previous = read_json_file(job_dir / STATUS_FILENAME) or {}
            request = read_json_file(job_dir / REQUEST_FILENAME) or {}
        else:
            previous = _read_private_job_json_at(job_dir_fd, STATUS_FILENAME) or {}
            request = _read_private_job_json_at(job_dir_fd, REQUEST_FILENAME) or {}
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
        status_redaction = redact_observability_payload(
            payload,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(job_dir.parent.parent),
        )
        payload = (
            status_redaction.value
            if isinstance(status_redaction.value, dict)
            else {
                "status": status,
                "message": "[TRUNCATED:JSON_LIMIT]",
                "stage": stage or status,
                "error_code": error_code,
                "updated_at": now,
            }
        )
        if status_redaction.truncated:
            payload["payload_truncated"] = True
            payload["truncation_reasons"] = list(
                status_redaction.truncation_reasons
            )
        if job_dir_fd is None:  # pragma: no cover - native Windows validation required
            write_json_atomic(job_dir / STATUS_FILENAME, payload)
        else:
            _write_private_job_json_at(job_dir_fd, STATUS_FILENAME, payload)
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
                "sanitization": {
                    "redacted_before_persistence": True,
                    "redacted": status_redaction.replacement_count > 0,
                    "payload_truncated": status_redaction.truncated,
                    "truncation_reasons": list(
                        status_redaction.truncation_reasons
                    ),
                },
            }
            events_path = job_dir / STATUS_EVENTS_FILENAME
            try:
                _append_private_status_event(
                    events_path,
                    event,
                    dir_fd=job_dir_fd,
                )
            except (OSError, ValueError) as exc:
                # Status is authoritative; an observability sink must not roll back
                # or invalidate a completed state transition.
                _LOGGER.warning(
                    "job status event append failed | job=%s error=%s",
                    job_dir.name,
                    type(exc).__name__,
                )
        return payload
    finally:
        if job_dir_fd is not None:
            os.close(job_dir_fd)


def _append_private_status_event(
    path: Path,
    event: dict[str, Any],
    *,
    dir_fd: int | None = None,
) -> None:
    encoded = _status_event_bytes(event)
    if len(encoded) > _MAX_STATUS_EVENT_BYTES:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        event = {
            "event": str(event.get("event") or "job_stage_changed"),
            "recorded_at": event.get("recorded_at"),
            "data": {
                key: (str(data.get(key) or "")[:2000] if key == "message" else data.get(key))
                for key in (
                    "previous_status",
                    "previous_stage",
                    "status",
                    "stage",
                    "error_code",
                    "message",
                    "attempt",
                    "created_at",
                    "updated_at",
                    "heartbeat_at",
                )
            },
            "payload_truncated": True,
            "original_bytes": len(encoded),
            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
            "sanitization": {
                **(
                    event.get("sanitization")
                    if isinstance(event.get("sanitization"), dict)
                    else {}
                ),
                "redacted_before_persistence": True,
                "payload_truncated": True,
            },
        }
        encoded = _status_event_bytes(event)
    if len(encoded) > _MAX_STATUS_EVENT_BYTES:
        raise ValueError("bounded job status event exceeds the hard size limit")

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = (
        os.open(path, flags, 0o600)
        if dir_fd is None
        else os.open(path.name, flags, 0o600, dir_fd=dir_fd)
    )
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise OSError("job status event log must be a single-link regular file")
        if os.name == "posix" and current.st_uid != os.geteuid():
            raise OSError("job status event log has an unexpected owner")
        unsafe_permissions = stat.S_IMODE(current.st_mode) & 0o077
        if unsafe_permissions:
            if unsafe_permissions & 0o022:
                raise OSError("job status event log has unsafe writable permissions")
            if not hasattr(os, "fchmod"):
                raise OSError("job status event log has unsafe permissions")
            os.fchmod(fd, 0o600)
            current = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (os.name == "posix" and current.st_uid != os.geteuid())
                or stat.S_IMODE(current.st_mode) & 0o077
            ):
                raise OSError("job status event log could not be made private")
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("failed to append job status event")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def _status_event_bytes(event: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def read_job_result(job: BackgroundJob) -> dict[str, Any] | None:
    try:
        return _read_private_job_json(job.job_dir, RESULT_FILENAME)
    except FileNotFoundError:
        return None
    except _JobJsonTooLargeError as exc:
        # A terminal worker artifact must remain observable even when a tool
        # produced an unexpectedly large result.  Return a bounded manifest so
        # the watcher can deliver/merge a terminal failure instead of polling
        # the same oversized file forever.
        return {
            "job_id": job.job_id,
            "tool_name": job.tool_name,
            "ok": False,
            "summary": "Job result artifact exceeded the 8 MiB safety limit.",
            "outputs": [],
            "error": "The complete result was omitted because it exceeded the safety limit.",
            "error_code": "result_artifact_too_large",
            "details": {
                "payload_truncated": True,
                "artifact_size_bytes": exc.size_bytes,
                "max_bytes": _MAX_JOB_JSON_BYTES,
            },
            "stage": "failed",
            "payload_truncated": True,
        }
    except OSError:
        # Missing artifacts return ``None`` from the bounded reader.  Reaching
        # this branch means a result exists but is unsafe or malformed; surface
        # a body-free terminal manifest so the background watcher cannot spin
        # forever on a poisoned artifact.
        return {
            "job_id": job.job_id,
            "tool_name": job.tool_name,
            "ok": False,
            "summary": "Job result artifact failed integrity validation.",
            "outputs": [],
            "error": "The result body was omitted because its artifact is unsafe or malformed.",
            "error_code": "result_artifact_unsafe",
            "details": {"payload_omitted": True, "integrity_gap": True},
            "stage": "failed",
            "payload_truncated": True,
        }


def read_job_status(job: BackgroundJob) -> dict[str, Any] | None:
    return _read_job_artifact(job.job_dir, STATUS_FILENAME)


def read_job_notification(job: BackgroundJob) -> dict[str, Any] | None:
    return _read_job_artifact(job.job_dir, NOTIFICATION_FILENAME)


def find_job(workspace: Workspace, job_id: str) -> BackgroundJob | None:
    safe_job_id = safe_segment(job_id)
    if safe_job_id != job_id:
        return None
    job_dir = job_storage_root(workspace) / safe_job_id
    if not job_dir.is_dir():
        return None
    return _job_from_dir(job_dir, workspace)


def is_job_completed(job: BackgroundJob) -> bool:
    status = read_job_status(job) or {}
    state = str(status.get("status") or "")
    if state:
        return state in COMPLETED_STATUSES
    return read_job_result(job) is not None


def latest_code_job(
    workspace: Workspace,
    *,
    user_id: str | None = None,
) -> BackgroundJob | None:
    jobs_root = job_storage_root(workspace)
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
    request = _read_job_artifact(job.job_dir, REQUEST_FILENAME) or {}
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
    jobs_root = job_storage_root(workspace)
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
    request = _read_job_artifact(job.job_dir, REQUEST_FILENAME) or {}
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
        scope=str(notify.get("scope") or workspace_payload.get("scope") or "actor").strip() or "actor",
    )


def _job_from_dir(job_dir: Path, workspace: Workspace) -> BackgroundJob | None:
    request = _read_job_artifact(job_dir, REQUEST_FILENAME) or {}
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
    "iter_job_request_paths",
    "job_storage_root",
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
