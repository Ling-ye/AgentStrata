"""Per-turn task progress records for the console UI."""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from chatcopilot.contracts.identity import stable_actor_ref
from chatcopilot.contracts.persona_control import PersonaDraftResult
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.jobs import read_json_file, write_json_atomic
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets,
    default_observability_roots,
    load_bounded_observability_json,
    omit_local_resource_paths,
    omit_private_reasoning_messages,
    redact_observability_payload,
)
from chatcopilot.middleware.runtime.task_forecast import (
    FORECAST_VERSION,
    forecast_llm_usage,
    forecast_task_usage,
    load_task_history,
    normalize_usage,
)
from chatcopilot.core.workspace_runtime import Workspace

TASK_SCHEMA_VERSION = 2
TASKS_DIRNAME = "tasks"
TASK_FILENAME = "task.json"
EVENTS_FILENAME = "events.jsonl"
EVENT_SEQUENCE_FILENAME = ".events.sequence"
COMPLETION_LOCK_FILENAME = ".completion.lock"
TURN_FILENAME = "turn.json"
CONTEXTS_DIRNAME = "contexts"
GROUP_TASK_ACTORS_DIRNAME = "task-actors"
GROUP_TASK_INTAKE_DIRNAME = "task-intake"
MAX_CONTEXT_ARTIFACT_BYTES = 8 * 1024 * 1024
ACTIVITY_SUMMARY_WRITE_INTERVAL_SECONDS = 0.25
MAX_PROVIDER_ACTIVITY_SUMMARIES = 500
MAX_PROVIDER_ACTIVITY_RAW_EVENTS = MAX_PROVIDER_ACTIVITY_SUMMARIES * 2
MAX_TASK_TOOL_SUMMARIES = 1000
MAX_TASK_STEP_SUMMARIES = 1000
MAX_TASK_LLM_CALL_SUMMARIES = 1000
MAX_TASK_CONTEXT_SNAPSHOT_SUMMARIES = 5000
MAX_TASK_INPUT_RESOURCE_SUMMARIES = 500
MAX_INPUT_RESOURCES_PER_SUMMARY = 20
MAX_TASK_EVENT_BYTES = 64 * 1024
MAX_EVENT_SEQUENCE = (1 << 63) - 1
MAX_USAGE_TOTAL = (1 << 63) - 1
MAX_TASK_SUMMARY_BYTES = 8 * 1024 * 1024
MAX_JOB_RESULT_SUMMARIES = 1000
MAX_JOB_RESULT_OUTPUTS = 8
MAX_JOB_RESULT_TEXT_CHARS = 1024
MAX_JOB_RESULT_OUTPUT_CHARS = 512
_MAX_EVENT_SEQUENCE_STATE_BYTES = len(str(MAX_EVENT_SEQUENCE))
_EVENT_LOCK_TIMEOUT_SECONDS = 0.25
_COMPLETION_LOCK_TIMEOUT_SECONDS = 5.0
_LOGGER = logging.getLogger(__name__)
_PROVIDER_ACTIVITY_KINDS = frozenset(
    {
        "command",
        "reasoning",
        "mcp_tool",
        "web_search",
        "file_change",
        "plan",
        "provider_event",
    }
)
_PROVIDER_OMISSION_KIND = "provider_omission"
_JOB_ID_RE = re.compile(r"\bjob_\d{8}_\d{6}_[0-9a-fA-F]{8}\b")
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_.-]{1,159}$")
_CONTEXT_ID_RE = re.compile(r"^ctx_[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_ARTIFACT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_TRUNCATED_ORIGINAL_CHARS_RE = re.compile(r"\[ORIGINAL_CHARS=(\d+)\]")


def make_task_id(now: Optional[float] = None) -> str:
    ts = time.localtime(time.time() if now is None else now)
    return f"task_{time.strftime('%Y%m%d_%H%M%S', ts)}_{uuid.uuid4().hex[:8]}"


def describe_user_text(text: str, *, limit: int = 120) -> str:
    first_line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    if not first_line:
        return "（空消息）"
    return first_line if len(first_line) <= limit else first_line[: limit - 1] + "…"


def group_task_actor_root(workspace: Workspace, *, create: bool = False) -> Path:
    """Return the protected per-actor observability root for a shared group.

    The shared workspace is intentionally member-writable.  Turn diagnostics
    can contain tool summaries, model metadata and host-path receipts, so they
    must live in the protected conversation sibling and remain partitioned by
    the authenticated transport actor.  The raw actor ID never becomes a path
    segment.
    """

    if workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        return workspace.root
    if workspace.root.name != "shared" or not workspace.chat_id or not workspace.user_id:
        raise ValueError("shared-group task storage requires stable chat and actor identities")
    actor_digest = hashlib.sha256(
        (f"{workspace.chat_kind or 'group'}\0{workspace.chat_id}\0{workspace.user_id}").encode(
            "utf-8"
        )
    ).hexdigest()
    state_root = workspace.root.parent / ".conversation-state"
    actors_root = state_root / GROUP_TASK_ACTORS_DIRNAME
    actor_root = actors_root / actor_digest
    if create:
        for path in (state_root, actors_root, actor_root):
            if path.is_symlink():
                raise RuntimeError("protected group task directory must not be a symlink")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.is_dir() or path.is_symlink():
                raise RuntimeError("protected group task path must be a real directory")
            path.chmod(0o700)
            info = path.stat()
            if os.name == "posix" and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise RuntimeError("protected group task directory must be owner-only")
    return actor_root


def group_task_intake_root(workspace: Workspace, *, create: bool = False) -> Path:
    """Return protected storage for a shared-group message without trusted actor ID.

    Identity-rejected inbound messages still need an auditable Console task, but
    their untrusted sender envelope must never choose an actor partition.  This
    group-level intake root stores only a generic, redacted rejection record.
    """

    if workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        return workspace.root
    if workspace.root.name != "shared" or not workspace.chat_id:
        raise ValueError("shared-group intake storage requires a stable chat identity")
    state_root = workspace.root.parent / ".conversation-state"
    intake_root = state_root / GROUP_TASK_INTAKE_DIRNAME
    if create:
        for path in (state_root, intake_root):
            if path.is_symlink():
                raise RuntimeError("protected group intake directory must not be a symlink")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.is_dir() or path.is_symlink():
                raise RuntimeError("protected group intake path must be a real directory")
            path.chmod(0o700)
            info = path.stat()
            if os.name == "posix" and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise RuntimeError("protected group intake directory must be owner-only")
    return intake_root


def _workspace_payload(
    workspace: Workspace,
    *,
    redact_identity: bool = False,
) -> Dict[str, Any]:
    shared_group = workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
    actor_ref = (
        stable_actor_ref(
            "qq",
            workspace.user_id or "",
            conversation_id=f"{workspace.chat_kind or ''}:{workspace.chat_id or ''}",
        )
        if workspace.user_id and (shared_group or redact_identity)
        else None
    )
    return {
        "root": str(workspace.root),
        "chat_kind": workspace.chat_kind,
        "chat_id": None if shared_group or redact_identity else workspace.chat_id,
        "user_id": None if shared_group or redact_identity else workspace.user_id,
        "user_name": None if shared_group or redact_identity else workspace.user_name,
        "actor_ref": actor_ref,
    }


def _resolve_task_observability_root(
    workspace: Workspace,
    history_root: Path | None,
) -> Path:
    configured_workspace = workspace.root.expanduser()
    try:
        workspace_info = configured_workspace.lstat()
    except FileNotFoundError:
        workspace_info = None
    if workspace_info is not None and (
        stat.S_ISLNK(workspace_info.st_mode)
        or not stat.S_ISDIR(workspace_info.st_mode)
        or (os.name == "posix" and workspace_info.st_uid != os.geteuid())
    ):
        raise ValueError("task workspace root must be a real directory owned by the current user")
    workspace_root = configured_workspace.resolve()
    if history_root is None:
        return workspace_root
    configured_root = history_root.expanduser()
    try:
        configured_info = configured_root.lstat()
    except FileNotFoundError:
        configured_info = None
    if configured_info is not None and (
        stat.S_ISLNK(configured_info.st_mode)
        or not stat.S_ISDIR(configured_info.st_mode)
        or (os.name == "posix" and configured_info.st_uid != os.geteuid())
    ):
        raise ValueError("task history root must be a real directory owned by the current user")
    trusted_root = configured_root.resolve()
    try:
        workspace_root.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("task workspace must be contained by its history root") from exc
    return trusted_root


def _materialize_private_task_workspace(
    workspace: Workspace,
    *,
    history_root: Path | None,
    observability_root: Path,
) -> None:
    """Create only a private p2p task root when the admitted workspace is still lazy."""

    configured_workspace = workspace.root.expanduser()
    try:
        workspace_info = configured_workspace.lstat()
    except FileNotFoundError:
        workspace_info = None
    if workspace_info is not None:
        if (
            stat.S_ISLNK(workspace_info.st_mode)
            or not stat.S_ISDIR(workspace_info.st_mode)
            or (os.name == "posix" and workspace_info.st_uid != os.geteuid())
        ):
            raise OSError("task workspace root is unsafe")
        return
    workspace_root = configured_workspace.resolve()
    if history_root is None:
        raise OSError("missing task history root for an unmaterialized workspace")

    observability_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_info = observability_root.lstat()
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or (os.name == "posix" and root_info.st_uid != os.geteuid())
    ):
        raise OSError("task history root is unsafe")

    relative = workspace_root.relative_to(observability_root)
    if os.name != "posix":  # pragma: no cover - native Windows validation required
        current = observability_root
        for part in relative.parts:
            current = current / part
            try:
                current_info = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                current_info = current.lstat()
            if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISDIR(current_info.st_mode):
                raise OSError("task workspace path is unsafe")
            _chmod_private(current, 0o700)
        return

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(observability_root, flags)
    try:
        current = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_uid != os.geteuid()
            or (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino)
        ):
            raise OSError("task history root identity is unsafe")
        for part in relative.parts:
            child_fd = _open_private_child_dir_at(directory_fd, part, create=True)
            os.close(directory_fd)
            directory_fd = child_fd
    finally:
        os.close(directory_fd)


def _replace_identity_literals(value: Any, literals: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        safe = value
        for literal in sorted(set(literals), key=len, reverse=True):
            safe = safe.replace(literal, "[REDACTED_IDENTITY]")
        return safe
    if isinstance(value, list):
        return [_replace_identity_literals(item, literals) for item in value]
    if isinstance(value, dict):
        return {
            _replace_identity_literals(key, literals): _replace_identity_literals(
                item,
                literals,
            )
            for key, item in value.items()
        }
    return value


def _redact_workspace_identity(
    payload: Any,
    workspace: Workspace,
    *,
    force: bool = False,
) -> Any:
    if not force and workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        return payload
    literals = tuple(
        value
        for value in (
            workspace.chat_id,
            workspace.user_id,
            workspace.user_name,
        )
        if value
    )
    return _replace_identity_literals(payload, literals)


def _redact_group_turn_content(
    payload: Any,
    workspace: Workspace,
    *,
    user_text: str,
    message_id: str | None,
) -> Any:
    if workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        return payload

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            safe = value
            if message_id:
                safe = safe.replace(message_id, "[REDACTED_MESSAGE]")
            if user_text:
                safe = safe.replace(user_text, "[REDACTED_GROUP_TURN_TEXT]")
            return safe
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    return redact(payload)


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


def _saturating_nonnegative_add(left: Any, right: Any) -> int:
    def bounded(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return min(value, MAX_USAGE_TOTAL)

    return min(MAX_USAGE_TOTAL, bounded(left) + bounded(right))


def _open_private_child_dir_at(
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> int:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise OSError("private directory name is invalid")
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
        if not stat.S_ISDIR(current.st_mode):
            raise OSError("private observability directory is not a directory")
        if os.name == "posix" and current.st_uid != os.geteuid():
            raise OSError("private observability directory has an unexpected owner")
        if stat.S_IMODE(current.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
            current = os.fstat(fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (os.name == "posix" and current.st_uid != os.geteuid())
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise OSError("private observability directory could not be secured")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_task_dir(task_dir: Path, *, create: bool) -> int | None:
    task_id = task_dir.name
    tasks_dir = task_dir.parent
    workspace_root = tasks_dir.parent
    if tasks_dir.name != TASKS_DIRNAME or not _TASK_ID_RE.fullmatch(task_id):
        raise OSError("task observability path is invalid")

    if os.name != "posix":  # pragma: no cover - native Windows validation required
        workspace_stat = workspace_root.lstat()
        if stat.S_ISLNK(workspace_stat.st_mode) or not stat.S_ISDIR(workspace_stat.st_mode):
            raise OSError("workspace root must be a real directory")
        for directory in (tasks_dir, task_dir):
            try:
                current = directory.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                directory.mkdir(mode=0o700)
                current = directory.lstat()
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
                raise OSError("private observability directory must be a real directory")
            _chmod_private(directory, 0o700)
            _require_private_path(directory, mode=0o700, directory=True)
        return None

    expected_root = workspace_root.lstat()
    if stat.S_ISLNK(expected_root.st_mode) or not stat.S_ISDIR(expected_root.st_mode):
        raise OSError("workspace root must be a real directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(workspace_root, flags)
    tasks_fd: int | None = None
    try:
        current_root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or (current_root.st_dev, current_root.st_ino)
            != (expected_root.st_dev, expected_root.st_ino)
            or current_root.st_uid != os.geteuid()
        ):
            raise OSError("workspace root identity is unsafe")
        tasks_fd = _open_private_child_dir_at(
            root_fd,
            TASKS_DIRNAME,
            create=create,
        )
        return _open_private_child_dir_at(tasks_fd, task_id, create=create)
    finally:
        if tasks_fd is not None:
            os.close(tasks_fd)
        os.close(root_fd)


def _private_json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _write_private_json_at(dir_fd: int, name: str, payload: Dict[str, Any]) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise OSError("private artifact name is invalid")
    encoded = _private_json_bytes(payload)
    if len(encoded) > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("private observability artifact exceeds the hard size limit")
    temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
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
                raise OSError("private temporary artifact is unsafe")
            os.fchmod(fd, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("failed to write private observability artifact")
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
            or (os.name == "posix" and existing.st_uid != os.geteuid())
        ):
            raise OSError("existing private observability artifact is unsafe")
        os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp_exists = False
    finally:
        if temp_exists:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


def _read_private_json_at(
    dir_fd: int,
    name: str,
    *,
    max_bytes: int = MAX_TASK_SUMMARY_BYTES,
) -> Dict[str, Any] | None:
    if not name or Path(name).name != name or name in {".", ".."}:
        return None
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
            or current.st_size > max_bytes
            or (os.name == "posix" and current.st_uid != os.geteuid())
        ):
            return None
        chunks: list[bytes] = []
        remaining = current.st_size + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if sum(len(chunk) for chunk in chunks) > max_bytes:
            return None
        loaded = load_bounded_observability_json(
            b"".join(chunks),
            max_bytes=max_bytes,
        )
        if not loaded.ok:
            return None
        payload = loaded.value
    except OSError:
        return None
    finally:
        os.close(fd)
    return payload if isinstance(payload, dict) else None


def _read_private_task_json(task_dir: Path, name: str) -> Dict[str, Any] | None:
    task_dir_fd = _open_private_task_dir(task_dir, create=False)
    try:
        if task_dir_fd is None:  # pragma: no cover - native Windows validation required
            return read_json_file(task_dir / name)
        return _read_private_json_at(task_dir_fd, name)
    finally:
        if task_dir_fd is not None:
            os.close(task_dir_fd)


def _write_private_task_json(
    task_dir: Path,
    name: str,
    payload: Dict[str, Any],
    *,
    create: bool = False,
) -> None:
    bounded_payload = _bounded_task_or_turn_document(name, payload)
    task_dir_fd = _open_private_task_dir(task_dir, create=create)
    try:
        if task_dir_fd is None:  # pragma: no cover - native Windows validation required
            target = task_dir / name
            write_json_atomic(target, bounded_payload)
            _chmod_private(target, 0o600)
            _require_private_path(target, mode=0o600, directory=False)
        else:
            _write_private_json_at(task_dir_fd, name, bounded_payload)
    finally:
        if task_dir_fd is not None:
            os.close(task_dir_fd)


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
        default_factory=_empty_usage_totals, init=False, repr=False
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
        self._observability_root = _resolve_task_observability_root(
            self.workspace,
            self.history_root,
        )
        if self.unauthenticated_intake and self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED:
            storage_root = group_task_intake_root(self.workspace, create=True)
        else:
            if self.workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
                _materialize_private_task_workspace(
                    self.workspace,
                    history_root=self.history_root,
                    observability_root=self._observability_root,
                )
            storage_root = group_task_actor_root(
                self.workspace,
                create=self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED,
            )
        self._path = storage_root / TASKS_DIRNAME / self.task_id / TASK_FILENAME
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
        with _task_completion_lock(self._path.parent, create=True):
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

        persisted = _read_private_task_json(self._path.parent, TASK_FILENAME)
        if not isinstance(persisted, dict):
            return

        ordered_ids = [
            job_id
            for job_id in self._job_ids
            if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id)
        ]
        for raw_job_id in persisted.get("job_ids") or []:
            job_id = str(raw_job_id or "")
            if _JOB_ID_RE.fullmatch(job_id) and job_id not in ordered_ids:
                ordered_ids.append(job_id)

        summaries: Dict[str, Dict[str, Any]] = {}
        for source in (self._job_results, persisted.get("job_results") or []):
            if not isinstance(source, list):
                continue
            for raw_summary in source:
                if not isinstance(raw_summary, dict):
                    continue
                job_id = str(raw_summary.get("job_id") or "")
                if not _JOB_ID_RE.fullmatch(job_id):
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
        safe_payload = self._sanitize_for_persistence(payload)
        _write_private_task_json(
            self._path.parent,
            TASK_FILENAME,
            safe_payload,
            create=True,
        )
        self._last_summary_write_at = time.monotonic()

    def _write_activity_progress(self, progress: str) -> None:
        """Bound task.json rewrites while raw activity events remain append-only."""
        self._progress = progress
        if time.monotonic() - self._last_summary_write_at < ACTIVITY_SUMMARY_WRITE_INTERVAL_SECONDS:
            return
        self.write()

    def _sanitize_for_persistence(self, payload: Any) -> Any:
        prepared = payload
        if not self.redact_identity:
            prepared = _redact_group_turn_content(
                payload,
                self.workspace,
                user_text=self.user_text,
                message_id=self.message_id,
            )
        if self.redact_identity and self.message_id:
            prepared = _replace_identity_literals(prepared, (self.message_id,))
        result = redact_observability_payload(
            prepared,
            secrets=collect_observability_secrets(),
            roots=default_observability_roots(self._observability_root),
        )
        return _redact_workspace_identity(
            result.value,
            self.workspace,
            force=self.redact_identity,
        )

    def _persisted_user_text(self) -> str:
        if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED and not self.redact_identity:
            return "（群消息正文未保存）"
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
            round(ended - float(started_at), 4) if isinstance(started_at, (int, float)) else None
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
                    totals[key] = _saturating_nonnegative_add(
                        totals.get(key, 0),
                        child_usage.get(key, 0),
                    )
            totals = normalize_usage(totals)
            step["inclusive_usage"] = totals
            return totals

        for item in self._steps:
            inclusive(item, set())

    def _accumulate_usage(self, usage: Dict[str, Any]) -> None:
        normalized = _normalize_usage_payload(usage)
        self._usage_totals["llm_calls"] = _saturating_nonnegative_add(
            self._usage_totals.get("llm_calls", 0),
            1,
        )
        if normalized.get("cached_tokens", 0) > 0 or normalized.get("cache_read_tokens", 0) > 0:
            self._usage_totals["cache_hit_calls"] = _saturating_nonnegative_add(
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
            self._usage_totals[key] = _saturating_nonnegative_add(
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
        if kind == _PROVIDER_OMISSION_KIND:
            self._append_event("span_started", entry)
            return
        is_provider_activity = kind in _PROVIDER_ACTIVITY_KINDS
        retain_summary = True
        if is_provider_activity:
            self._provider_activity_total = _saturating_nonnegative_add(
                self._provider_activity_total,
                1,
            )
            retained = sum(
                1 for item in self._tools if str(item.get("kind") or "") in _PROVIDER_ACTIVITY_KINDS
            )
            retain_summary = retained < MAX_PROVIDER_ACTIVITY_SUMMARIES
            if not retain_summary:
                self._provider_activity_dropped = _saturating_nonnegative_add(
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
        if kind == _PROVIDER_OMISSION_KIND:
            raw_count = (data or {}).get("omitted_count")
            omitted_count = (
                raw_count
                if isinstance(raw_count, int)
                and not isinstance(raw_count, bool)
                and 0 < raw_count <= (1 << 63) - 1
                else 0
            )
            self._provider_activity_total = _saturating_nonnegative_add(
                self._provider_activity_total,
                omitted_count,
            )
            self._provider_activity_dropped = _saturating_nonnegative_add(
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
            is_provider_activity = kind in _PROVIDER_ACTIVITY_KINDS
            retained = sum(
                1 for item in self._tools if str(item.get("kind") or "") in _PROVIDER_ACTIVITY_KINDS
            )
            if is_provider_activity and retained >= MAX_PROVIDER_ACTIVITY_SUMMARIES:
                self._append_provider_activity_omission_event()
                self._write_activity_progress(f"{name} 完成。")
                return
            target = {"name": name, "kind": kind, "started_at": None, "depth": depth}
            if span_id:
                target["span_id"] = span_id
            self._tools.append(target)
            if is_provider_activity:
                self._provider_activity_total = _saturating_nonnegative_add(
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
        backend: str,
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

        if not _CONTEXT_ID_RE.fullmatch(snapshot_id):
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
            "backend": backend,
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
        prepared_payload = _redact_group_turn_content(
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
        identity_safe_value = _redact_workspace_identity(
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
                "backend": backend,
                "model": model,
                "iteration": iteration,
            }
        )
        safe_payload["coverage"] = effective_coverage
        safe_payload["omitted"] = effective_omitted
        canonical = _json_bytes(safe_payload)
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
        if payload_truncated or len(_json_bytes(safe_payload)) > MAX_CONTEXT_ARTIFACT_BYTES:
            safe_payload = _truncated_context_payload(
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
            "backend": backend,
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
            contexts_dir = self._path.parent / CONTEXTS_DIRNAME
            target = contexts_dir / f"{snapshot_id}.json"
            task_dir_fd = _open_private_task_dir(self._path.parent, create=False)
            contexts_fd: int | None = None
            try:
                if task_dir_fd is None:  # pragma: no cover - native Windows validation required
                    if contexts_dir.is_symlink():
                        raise OSError("context artifact directory must not be a symlink")
                    contexts_dir.mkdir(mode=0o700, exist_ok=True)
                    _chmod_private(contexts_dir, 0o700)
                    _require_private_path(contexts_dir, mode=0o700, directory=True)
                    write_json_atomic(target, safe_payload)
                    _chmod_private(target, 0o600)
                    _require_private_path(target, mode=0o600, directory=False)
                else:
                    contexts_fd = _open_private_child_dir_at(
                        task_dir_fd,
                        CONTEXTS_DIRNAME,
                        create=True,
                    )
                    _write_private_json_at(
                        contexts_fd,
                        f"{snapshot_id}.json",
                        safe_payload,
                    )
            finally:
                if contexts_fd is not None:
                    os.close(contexts_fd)
                if task_dir_fd is not None:
                    os.close(task_dir_fd)
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
        backend: str,
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
        valid_id = snapshot_id if _CONTEXT_ID_RE.fullmatch(snapshot_id) else ""
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
            "backend": backend or "unknown",
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
        backend: str,
        turn_index: int,
        request_id: str,
        resources: List[Dict[str, Any]],
    ) -> None:
        receipt = {
            "backend": backend,
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
        backend: str = "",
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
            backend=backend,
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
            "backend": backend,
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
        backend: str = "",
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
        normalized = _normalize_usage_payload(usage or {})
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
            "backend": backend,
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
                backend=backend,
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
            normalized = _normalize_usage_payload(usage)
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
            normalized = _normalize_usage_payload(dict(call.usage or {}))
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
            target_dir = self._path.parent / "subagents"
            artifact_stem = (
                span_id
                if _ARTIFACT_SEGMENT_RE.fullmatch(span_id)
                else f"span_{hashlib.sha256(span_id.encode('utf-8')).hexdigest()[:24]}"
            )
            target = target_dir / f"{artifact_stem}.json"
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
            task_dir_fd = _open_private_task_dir(self._path.parent, create=False)
            subagents_fd: int | None = None
            try:
                if task_dir_fd is None:  # pragma: no cover - native Windows validation required
                    if target_dir.is_symlink():
                        raise OSError("subagent artifact directory must not be a symlink")
                    target_dir.mkdir(mode=0o700, exist_ok=True)
                    _chmod_private(target_dir, 0o700)
                    _require_private_path(target_dir, mode=0o700, directory=True)
                    write_json_atomic(target, payload)
                    _chmod_private(target, 0o600)
                    _require_private_path(target, mode=0o600, directory=False)
                else:
                    subagents_fd = _open_private_child_dir_at(
                        task_dir_fd,
                        "subagents",
                        create=True,
                    )
                    _write_private_json_at(subagents_fd, target.name, payload)
            finally:
                if subagents_fd is not None:
                    os.close(subagents_fd)
                if task_dir_fd is not None:
                    os.close(task_dir_fd)
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
        try:
            with _task_completion_lock(self._path.parent, create=True):
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
                    self._progress = _delegated_progress(
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
                    self._progress = _delegated_progress(
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
                safe_turn = self._sanitize_for_persistence(turn)
                _write_private_task_json(
                    self._path.parent,
                    TURN_FILENAME,
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
                "retained_summary_limit": MAX_PROVIDER_ACTIVITY_SUMMARIES,
                "retained_raw_event_limit": MAX_PROVIDER_ACTIVITY_RAW_EVENTS,
            },
        )

    def _append_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self._event_lock:
            _append_task_event(
                self._path.parent,
                event_type,
                self._sanitize_for_persistence(payload),
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
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "description": (
                "（群消息正文未保存）"
                if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED and not self.redact_identity
                else describe_user_text(self.user_text)
            ),
            "progress": self._progress,
            "status": self._status,
            "submitter": (
                stable_actor_ref(
                    "qq",
                    self.workspace.user_id,
                    conversation_id=(
                        f"{self.workspace.chat_kind or ''}:{self.workspace.chat_id or ''}"
                    ),
                )
                if self.workspace.user_id
                and (self.redact_identity or self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED)
                else "未验证来源"
                if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
                else self.workspace.user_name or self.workspace.user_id or ""
            ),
            "asked_at": self.asked_at,
            "started_at": self.asked_at,
            "finished_at": finished_at,
            "turn_finished_at": self._turn_finished_at,
            "elapsed_s": elapsed_s,
            "updated_at": updated_at,
            "tools": _task_tool_summaries(self._tools),
            "llm_calls": self._llm_calls,
            "context_snapshots": self._context_snapshots,
            "input_resources": self._input_resources,
            "steps": _task_step_summaries(self._steps),
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
                "tools_retained": min(len(self._tools), MAX_TASK_TOOL_SUMMARIES),
                "steps_total": len(self._steps),
                "steps_retained": min(len(self._steps), MAX_TASK_STEP_SUMMARIES),
                "truncated": (
                    len(self._tools) > MAX_TASK_TOOL_SUMMARIES
                    or len(self._steps) > MAX_TASK_STEP_SUMMARIES
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
            "workspace": _workspace_payload(
                self.workspace,
                redact_identity=self.redact_identity,
            ),
            "path": str(self._path.parent),
            "trace_id": self.task_id,
            "events_path": str(self._path.parent / EVENTS_FILENAME),
            "turn_path": str(self._path.parent / TURN_FILENAME),
        }


def _bounded_string(value: Any, limit: int) -> tuple[str, bool]:
    try:
        text = str(value or "")
    except (ValueError, RecursionError):
        return "[invalid text]", True
    return (text, False) if len(text) <= limit else (text[: limit - 1] + "…", True)


def _payload_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        encoded = b"[unserializable]"
    return hashlib.sha256(encoded).hexdigest()


def _bounded_job_result(item: Any, *, compact: bool = False) -> Dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    summary, summary_truncated = _bounded_string(
        source.get("summary"),
        MAX_JOB_RESULT_TEXT_CHARS,
    )
    error, error_truncated = _bounded_string(
        source.get("error"),
        MAX_JOB_RESULT_TEXT_CHARS,
    )
    output_values, output_total, invalid_outputs = _bounded_collection(
        source.get("outputs"),
        MAX_JOB_RESULT_OUTPUTS,
    )
    raw_outputs = [value for value in output_values if isinstance(value, str)]
    outputs: list[str] = []
    output_text_truncated = False
    for value in raw_outputs:
        bounded, was_truncated = _bounded_string(value, MAX_JOB_RESULT_OUTPUT_CHARS)
        outputs.append(bounded)
        output_text_truncated = output_text_truncated or was_truncated
    omissions = [
        field
        for field in source.get("omitted_fields") or []
        if field in {"summary", "error", "outputs"}
    ]
    if summary_truncated:
        omissions.append("summary")
    if error_truncated:
        omissions.append("error")
    if invalid_outputs or output_total > len(outputs) or output_text_truncated:
        omissions.append("outputs")
    if compact:
        omissions.extend(
            field
            for field, value in (("summary", summary), ("error", error), ("outputs", outputs))
            if value and field not in omissions
        )
        summary = ""
        error = ""
        outputs = []
    result = {
        "job_id": _bounded_text(source.get("job_id"), 256),
        "ok": bool(source.get("ok")),
        "status": _bounded_text(source.get("status"), 64),
        "stage": _bounded_text(source.get("stage"), 256),
        "error_code": _bounded_text(source.get("error_code"), 256),
        "summary": summary,
        "error": error,
        "outputs": outputs,
        "output_count": output_total,
        "finished_at": _bounded_observed_number(source.get("finished_at")),
    }
    if omissions or bool(source.get("payload_truncated")):
        result.update(
            {
                "payload_truncated": True,
                "omitted_fields": sorted(set(omissions)),
                "payload_sha256": str(source.get("payload_sha256") or _payload_digest(source)),
            }
        )
    return result


def _bounded_job_results(values: Any, *, compact: bool = False) -> list[Dict[str, Any]]:
    items, _, _ = _bounded_collection(values, MAX_JOB_RESULT_SUMMARIES)
    return [_bounded_job_result(item, compact=compact) for item in items]


def _bounded_collection(
    value: Any,
    limit: int,
    *,
    keep_latest: bool = False,
) -> tuple[list[Any], int, bool]:
    if not isinstance(value, (list, tuple)):
        return [], 0, value not in (None, [])
    total = len(value)
    retained = value[-limit:] if keep_latest and limit > 0 else value[:limit]
    return list(retained), total, total > limit


def _bounded_nonnegative_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_USAGE_TOTAL)


def _bounded_observed_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(-MAX_USAGE_TOTAL, min(value, MAX_USAGE_TOTAL))
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _task_llm_call_summaries(values: Any) -> tuple[list[Dict[str, Any]], int, bool]:
    items, total, truncated = _bounded_collection(
        values,
        MAX_TASK_LLM_CALL_SUMMARIES,
        keep_latest=True,
    )
    summaries: list[Dict[str, Any]] = []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        raw_usage = item.get("usage")
        usage: Dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        summaries.append(
            {
                "model": _bounded_text(item.get("model"), 512),
                "backend": _bounded_text(item.get("backend"), 128),
                "iteration": _bounded_nonnegative_integer(item.get("iteration")),
                "finish_reason": _bounded_text(item.get("finish_reason"), 512),
                "usage": _normalize_usage_payload(usage),
                "trace_id": _bounded_text(item.get("trace_id"), 256),
                "span_id": _bounded_text(item.get("span_id"), 256),
                "parent_span_id": _bounded_text(item.get("parent_span_id"), 256),
                "depth": _bounded_nonnegative_integer(item.get("depth")),
                "role": _bounded_text(item.get("role"), 32),
                "started_at": _bounded_observed_number(item.get("started_at")),
                "recorded_at": _bounded_observed_number(item.get("recorded_at")),
                "input_message_count": _bounded_nonnegative_integer(
                    item.get("input_message_count")
                ),
                "input_estimated_tokens": _bounded_nonnegative_integer(
                    item.get("input_estimated_tokens")
                ),
                "system_estimated_tokens": _bounded_nonnegative_integer(
                    item.get("system_estimated_tokens")
                ),
                "tool_schema_count": _bounded_nonnegative_integer(item.get("tool_schema_count")),
                "tool_schema_estimated_tokens": _bounded_nonnegative_integer(
                    item.get("tool_schema_estimated_tokens")
                ),
                "estimator_version": _bounded_text(item.get("estimator_version"), 128),
                "context_kind": _bounded_text(item.get("context_kind"), 128),
                "context_snapshot_id": _bounded_text(
                    item.get("context_snapshot_id"),
                    128,
                ),
                "step_id": _bounded_text(item.get("step_id"), 256),
                "ok": bool(item.get("ok", True)),
            }
        )
    return summaries, total, truncated


def _task_context_snapshot_summaries(
    values: Any,
    *,
    minimal: bool = False,
) -> tuple[list[Dict[str, Any]], int, bool]:
    items, total, truncated = _bounded_collection(
        values,
        MAX_TASK_CONTEXT_SNAPSHOT_SUMMARIES,
        keep_latest=True,
    )
    summaries: list[Dict[str, Any]] = []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        summary: Dict[str, Any] = {
            # This identity is the Console authorization/index key for the
            # separate context artifact and must survive total-size fallback.
            "snapshot_id": _bounded_text(item.get("snapshot_id"), 128),
            "capture_status": _bounded_text(item.get("capture_status"), 64),
            "backend": _bounded_text(item.get("backend"), 128),
            "model": _bounded_text(item.get("model"), 128 if minimal else 512),
            "coverage": _bounded_text(item.get("coverage"), 64),
            "truncated": bool(item.get("truncated")),
            "captured_at": _bounded_observed_number(item.get("captured_at")),
        }
        if not minimal:
            omitted_items, _, omitted_truncated = _bounded_collection(
                item.get("omitted"),
                20,
            )
            summary.update(
                {
                    "redacted": bool(item.get("redacted")),
                    "iteration": _bounded_nonnegative_integer(item.get("iteration")),
                    "message_count": _bounded_nonnegative_integer(item.get("message_count")),
                    "effective_message_count": _bounded_nonnegative_integer(
                        item.get("effective_message_count")
                    ),
                    "tool_schema_count": _bounded_nonnegative_integer(
                        item.get("tool_schema_count")
                    ),
                    "resource_count": _bounded_nonnegative_integer(item.get("resource_count")),
                    "estimated_tokens": _bounded_nonnegative_integer(item.get("estimated_tokens")),
                    "reasoning_effort": _bounded_text(
                        item.get("reasoning_effort"),
                        64,
                    ),
                    "context_kind": _bounded_text(item.get("context_kind"), 128),
                    "omitted": [_bounded_text(value, 128) for value in omitted_items],
                    "omitted_truncated": omitted_truncated,
                    "trace_id": _bounded_text(item.get("trace_id"), 256),
                    "span_id": _bounded_text(item.get("span_id"), 256),
                    "parent_span_id": _bounded_text(item.get("parent_span_id"), 256),
                    "depth": _bounded_nonnegative_integer(item.get("depth")),
                    "role": _bounded_text(item.get("role"), 32),
                }
            )
        summaries.append(summary)
    return summaries, total, truncated


def _task_input_resource_summaries(
    values: Any,
) -> tuple[list[Dict[str, Any]], int, bool]:
    items, total, truncated = _bounded_collection(
        values,
        MAX_TASK_INPUT_RESOURCE_SUMMARIES,
        keep_latest=True,
    )
    summaries: list[Dict[str, Any]] = []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        resources, resource_total, resource_truncated = _bounded_collection(
            item.get("resources"),
            MAX_INPUT_RESOURCES_PER_SUMMARY,
        )
        resource_summaries: list[Dict[str, Any]] = []
        for raw_resource in resources:
            resource = raw_resource if isinstance(raw_resource, dict) else {}
            resource_summaries.append(
                {
                    "sequence": _bounded_nonnegative_integer(resource.get("sequence")),
                    "media_type": _bounded_text(resource.get("media_type"), 128),
                    "size_bytes": _bounded_nonnegative_integer(resource.get("size_bytes")),
                    "sha256": _bounded_text(resource.get("sha256"), 128),
                    "dispatch": _bounded_text(resource.get("dispatch"), 64),
                }
            )
        summaries.append(
            {
                "backend": _bounded_text(item.get("backend"), 128),
                "turn_index": _bounded_nonnegative_integer(item.get("turn_index")),
                "request_id": _bounded_text(item.get("request_id"), 256),
                "recorded_at": _bounded_observed_number(item.get("recorded_at")),
                "resources": resource_summaries,
                "resource_count": resource_total,
                "resources_truncated": resource_truncated,
            }
        )
    return summaries, total, truncated


def _previous_collection_total(limits: Dict[str, Any], key: str, observed: int) -> int:
    previous = limits.get(f"{key}_total")
    if isinstance(previous, int) and not isinstance(previous, bool) and previous >= 0:
        return max(observed, min(previous, MAX_USAGE_TOTAL))
    return observed


def _task_forecast_summary(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    baseline = source.get("baseline") if isinstance(source.get("baseline"), dict) else None
    usage = source.get("usage") if isinstance(source.get("usage"), dict) else None
    return {
        "status": _bounded_text(source.get("status"), 64),
        "model": _bounded_text(source.get("model"), 512),
        "context_kind": _bounded_text(source.get("context_kind"), 128),
        "sample_count": _bounded_nonnegative_integer(source.get("sample_count")),
        "max_samples": _bounded_nonnegative_integer(source.get("max_samples")),
        "min_samples": _bounded_nonnegative_integer(source.get("min_samples")),
        "calibration_ratio": _bounded_observed_number(source.get("calibration_ratio")),
        "estimator_version": _bounded_text(source.get("estimator_version"), 128),
        "baseline": normalize_usage(baseline or {}) if baseline is not None else None,
        "usage": normalize_usage(usage or {}) if usage is not None else None,
        "fixed_at": _bounded_observed_number(source.get("fixed_at")),
    }


def _task_usage_summary(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    summary: Dict[str, Any] = _normalize_usage_payload(source)
    summary.update(
        {
            "llm_calls": _bounded_nonnegative_integer(source.get("llm_calls")),
            "cache_hit_calls": _bounded_nonnegative_integer(source.get("cache_hit_calls")),
            "cache_hit_rate": _bounded_observed_number(source.get("cache_hit_rate")) or 0.0,
            "cache_hit_call_rate": _bounded_observed_number(source.get("cache_hit_call_rate"))
            or 0.0,
        }
    )
    return summary


def _bounded_task_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    document_fields = {
        "schema_version",
        "task_id",
        "description",
        "progress",
        "status",
        "submitter",
        "asked_at",
        "started_at",
        "finished_at",
        "turn_finished_at",
        "elapsed_s",
        "updated_at",
        "tools",
        "llm_calls",
        "context_snapshots",
        "input_resources",
        "steps",
        "activity_summary",
        "summary_limits",
        "current_step",
        "usage_totals",
        "forecast",
        "primary_model",
        "context_kind",
        "persona_outcome",
        "job_ids",
        "job_results",
        "session_id",
        "message_id",
        "workspace",
        "path",
        "trace_id",
        "events_path",
        "turn_path",
    }
    unknown_field_count = sum(1 for key in payload if key not in document_fields)
    bounded = {key: value for key, value in payload.items() if key in document_fields}
    bounded["schema_version"] = _bounded_nonnegative_integer(bounded.get("schema_version"))
    for key, limit in (
        ("task_id", 256),
        ("description", 512),
        ("progress", 4096),
        ("status", 64),
        ("submitter", 512),
        ("current_step", 1024),
        ("primary_model", 512),
        ("context_kind", 256),
        ("session_id", 256),
        ("message_id", 256),
        ("trace_id", 256),
        ("path", 1024),
        ("events_path", 1024),
        ("turn_path", 1024),
    ):
        if key in bounded and bounded.get(key) is not None:
            bounded[key] = _bounded_text(bounded.get(key), limit)
    for key in (
        "asked_at",
        "started_at",
        "finished_at",
        "turn_finished_at",
        "elapsed_s",
        "updated_at",
    ):
        if key in bounded:
            bounded[key] = _bounded_observed_number(bounded.get(key))
    bounded["workspace"] = _bounded_mapping(bounded.get("workspace"))
    raw_persona_outcome = bounded.get("persona_outcome")
    persona_outcome = raw_persona_outcome if isinstance(raw_persona_outcome, dict) else {}
    bounded["persona_outcome"] = {
        "outcome": _bounded_text(persona_outcome.get("outcome"), 80),
        "error_code": _bounded_text(persona_outcome.get("error_code"), 120),
    }
    bounded["usage_totals"] = _task_usage_summary(bounded.get("usage_totals"))
    bounded["forecast"] = _task_forecast_summary(bounded.get("forecast"))
    raw_activity = bounded.get("activity_summary")
    activity: Dict[str, Any] = raw_activity if isinstance(raw_activity, dict) else {}
    bounded["activity_summary"] = {
        "provider_total": _bounded_nonnegative_integer(activity.get("provider_total")),
        "provider_retained": _bounded_nonnegative_integer(activity.get("provider_retained")),
        "provider_dropped": _bounded_nonnegative_integer(activity.get("provider_dropped")),
        "truncated": bool(activity.get("truncated")),
    }
    raw_tools = bounded.get("tools")
    tool_source = raw_tools if isinstance(raw_tools, (list, tuple)) else []
    observed_tool_total = len(tool_source)
    tool_values = [
        item if isinstance(item, dict) else {} for item in tool_source[-MAX_TASK_TOOL_SUMMARIES:]
    ]
    bounded["tools"] = _task_tool_summaries(tool_values)
    raw_steps = bounded.get("steps")
    step_source = raw_steps if isinstance(raw_steps, (list, tuple)) else []
    observed_step_total = len(step_source)
    step_values = [
        item if isinstance(item, dict) else {} for item in step_source[-MAX_TASK_STEP_SUMMARIES:]
    ]
    bounded["steps"] = _task_step_summaries(step_values)
    raw_job_id_values, raw_job_id_total, invalid_job_ids = _bounded_collection(
        bounded.get("job_ids"),
        MAX_JOB_RESULT_SUMMARIES,
    )
    raw_job_ids = [str(value) for value in raw_job_id_values]
    raw_job_result_values, raw_job_result_total, invalid_job_results = _bounded_collection(
        bounded.get("job_results"),
        MAX_JOB_RESULT_SUMMARIES,
    )
    raw_job_results = list(raw_job_result_values)
    bounded["job_ids"] = [_bounded_text(value, 256) for value in raw_job_ids]
    bounded["job_results"] = _bounded_job_results(raw_job_results)
    limits = _bounded_mapping(bounded.get("summary_limits"))
    if unknown_field_count:
        limits["unknown_fields_omitted"] = unknown_field_count
        limits["truncated"] = True
    llm_calls, llm_call_total, llm_calls_truncated = _task_llm_call_summaries(
        bounded.get("llm_calls")
    )
    contexts, context_total, contexts_truncated = _task_context_snapshot_summaries(
        bounded.get("context_snapshots")
    )
    input_resources, input_resource_total, input_resources_truncated = (
        _task_input_resource_summaries(bounded.get("input_resources"))
    )
    bounded["llm_calls"] = llm_calls
    bounded["context_snapshots"] = contexts
    bounded["input_resources"] = input_resources
    llm_call_total = _previous_collection_total(limits, "llm_calls", llm_call_total)
    context_total = _previous_collection_total(
        limits,
        "context_snapshots",
        context_total,
    )
    input_resource_total = _previous_collection_total(
        limits,
        "input_resources",
        input_resource_total,
    )
    tool_total = _previous_collection_total(limits, "tools", observed_tool_total)
    step_total = _previous_collection_total(limits, "steps", observed_step_total)
    job_truncated = (
        invalid_job_ids
        or invalid_job_results
        or raw_job_id_total > len(bounded["job_ids"])
        or raw_job_result_total > len(bounded["job_results"])
        or any(item.get("payload_truncated") for item in bounded["job_results"])
    )
    limits.update(
        {
            "job_ids_total": raw_job_id_total,
            "job_ids_retained": len(bounded["job_ids"]),
            "job_results_total": raw_job_result_total,
            "job_results_retained": len(bounded["job_results"]),
            "job_results_truncated": job_truncated,
            "tools_total": tool_total,
            "tools_retained": len(bounded["tools"]),
            "steps_total": step_total,
            "steps_retained": len(bounded["steps"]),
            "llm_calls_total": llm_call_total,
            "llm_calls_retained": len(llm_calls),
            "llm_calls_truncated": llm_calls_truncated or llm_call_total > len(llm_calls),
            "context_snapshots_total": context_total,
            "context_snapshots_retained": len(contexts),
            "context_snapshots_truncated": contexts_truncated or context_total > len(contexts),
            "input_resources_total": input_resource_total,
            "input_resources_retained": len(input_resources),
            "input_resources_truncated": input_resources_truncated
            or input_resource_total > len(input_resources),
        }
    )
    limits["truncated"] = bool(limits.get("truncated")) or any(
        (
            job_truncated,
            tool_total > len(bounded["tools"]),
            step_total > len(bounded["steps"]),
            limits["llm_calls_truncated"],
            limits["context_snapshots_truncated"],
            limits["input_resources_truncated"],
        )
    )
    bounded["summary_limits"] = limits
    encoded = _private_json_bytes(bounded)
    if len(encoded) <= MAX_TASK_SUMMARY_BYTES:
        return bounded

    original_bytes = len(encoded)
    original_sha256 = hashlib.sha256(encoded).hexdigest()
    for key in ("tools", "steps"):
        value = bounded.get(key)
        limits[f"{key}_payload_count"] = len(value) if isinstance(value, list) else 0
        bounded[key] = []
    minimal_contexts, _, _ = _task_context_snapshot_summaries(
        contexts,
        minimal=True,
    )
    bounded["context_snapshots"] = minimal_contexts
    limits["context_snapshots_retained"] = len(minimal_contexts)
    limits["context_snapshots_minimal"] = True
    limits.update(
        {
            "payload_truncated": True,
            "payload_original_bytes": original_bytes,
            "payload_sha256": original_sha256,
            "truncated": True,
        }
    )
    if len(_private_json_bytes(bounded)) <= MAX_TASK_SUMMARY_BYTES:
        return bounded

    bounded["job_results"] = _bounded_job_results(raw_job_results, compact=True)
    limits["job_result_content_omitted"] = True
    bounded["llm_calls"] = []
    bounded["input_resources"] = []
    limits["llm_calls_retained"] = 0
    limits["llm_calls_truncated"] = llm_call_total > 0
    limits["input_resources_retained"] = 0
    limits["input_resources_truncated"] = input_resource_total > 0
    if len(_private_json_bytes(bounded)) > MAX_TASK_SUMMARY_BYTES:
        raise ValueError("bounded task summary exceeds the hard size limit")
    return bounded


def _bounded_turn_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    bounded = dict(payload)
    omissions: list[str] = []
    for key, limit in (
        ("task_id", 256),
        ("session_id", 256),
        ("message_id", 256),
        ("stop_reason", 512),
        ("user_text", 1024 * 1024),
        ("final_text", 1024 * 1024),
        ("error", 1024 * 1024),
    ):
        if key not in bounded or bounded.get(key) is None:
            continue
        value, truncated = _bounded_string(bounded.get(key), limit)
        bounded[key] = value
        if truncated:
            omissions.append(key)
    resource_values, resource_total, invalid_resources = _bounded_collection(
        bounded.get("produced_resources"),
        1000,
    )
    raw_resources = [value for value in resource_values if isinstance(value, str)]
    bounded["produced_resources"] = [_bounded_text(value, 1024) for value in raw_resources]
    if invalid_resources or resource_total > len(bounded["produced_resources"]):
        omissions.append("produced_resources")
    raw_job_results, job_result_total, invalid_job_results = _bounded_collection(
        bounded.get("job_results"),
        MAX_JOB_RESULT_SUMMARIES,
    )
    bounded["job_results"] = _bounded_job_results(raw_job_results)
    if (
        invalid_job_results
        or job_result_total > len(bounded["job_results"])
        or any(item.get("payload_truncated") for item in bounded["job_results"])
    ):
        omissions.append("job_results")
    if omissions:
        bounded["payload_truncated"] = True
        bounded["omitted_fields"] = sorted(set(omissions))
        bounded["payload_sha256"] = _payload_digest(payload)
    if len(_private_json_bytes(bounded)) <= MAX_TASK_SUMMARY_BYTES:
        return bounded
    bounded["user_text"] = _bounded_text(bounded.get("user_text"), 256 * 1024)
    bounded["final_text"] = _bounded_text(bounded.get("final_text"), 256 * 1024)
    bounded["error"] = _bounded_text(bounded.get("error"), 256 * 1024)
    bounded["job_results"] = _bounded_job_results(raw_job_results, compact=True)
    bounded["payload_truncated"] = True
    bounded["omitted_fields"] = sorted(
        set(list(bounded.get("omitted_fields") or []) + ["large_content"])
    )
    if len(_private_json_bytes(bounded)) > MAX_TASK_SUMMARY_BYTES:
        raise ValueError("bounded turn artifact exceeds the hard size limit")
    return bounded


def _bounded_task_or_turn_document(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if name == TASK_FILENAME:
        return _bounded_task_document(payload)
    if name == TURN_FILENAME:
        return _bounded_turn_document(payload)
    return payload


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
        storage_root = group_task_actor_root(workspace, create=False)
        observability_root = _resolve_task_observability_root(workspace, history_root)
    except ValueError:
        return None
    task_dir = storage_root / TASKS_DIRNAME / task_id
    try:
        with _task_completion_lock(task_dir):
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
                _append_task_event(
                    task_dir,
                    "job_completed",
                    _redact_workspace_identity(
                        {"job_id": job_id, "result": completed_result},
                        workspace,
                    ),
                    workspace_root=observability_root,
                )
                if all_complete:
                    _append_task_event(
                        task_dir,
                        "task_finished",
                        _redact_workspace_identity(
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


@contextmanager
def _task_completion_lock(task_dir: Path, *, create: bool = False) -> Iterator[None]:
    task_dir_fd = _open_private_task_dir(task_dir, create=create)
    lock_fd: int | None = None
    try:
        lock_path = task_dir / COMPLETION_LOCK_FILENAME
        lock_fd = _open_event_path(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=task_dir_fd,
        )
        _require_private_event_fd(lock_fd, label="task completion lock")
        _acquire_bounded_file_lock(
            lock_fd,
            timeout_seconds=_COMPLETION_LOCK_TIMEOUT_SECONDS,
            label="task completion lock",
        )
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if task_dir_fd is not None:
            os.close(task_dir_fd)


def _merge_delegated_task_completion(
    workspace: Workspace,
    *,
    task_dir: Path,
    job_id: str,
    result: Dict[str, Any],
    observability_root: Path,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], bool] | None:
    task = _read_private_task_json(task_dir, TASK_FILENAME)
    if not isinstance(task, dict):
        return None
    turn = _read_private_task_json(task_dir, TURN_FILENAME) or {}
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
        summaries[job_id] = _job_result_summary(
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
        task["progress"] = _delegated_progress(ordered_results, len(ordered_ids))
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
        _write_private_task_json(
            task_dir,
            TASK_FILENAME,
            _redact_workspace_identity(task_redaction.value, workspace),
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
        _write_private_task_json(
            task_dir,
            TURN_FILENAME,
            _redact_workspace_identity(turn_redaction.value, workspace),
        )
    return (
        task,
        None if result_already_recorded else summaries[job_id],
        all_complete,
    )


def _job_result_summary(
    job_id: str,
    result: Dict[str, Any],
    *,
    omit_free_text: bool = False,
) -> Dict[str, Any]:
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    raw_summary = str(result.get("summary") or "")
    raw_error = str(result.get("error") or "")
    summary = _bounded_job_result(
        {
            "job_id": job_id,
            "ok": bool(result.get("ok")),
            "status": "succeeded" if result.get("ok") else "failed",
            "stage": str(
                details.get("failed_stage")
                or result.get("stage")
                or ("succeeded" if result.get("ok") else "failed")
            ),
            "error_code": str(result.get("error_code") or ""),
            "summary": "" if omit_free_text else raw_summary,
            "error": "" if omit_free_text else raw_error,
            "outputs": [str(item) for item in result.get("outputs") or [] if isinstance(item, str)],
            "finished_at": result.get("finished_at"),
        }
    )
    omitted_fields = [
        field
        for field, value in (("summary", raw_summary), ("error", raw_error))
        if omit_free_text and value
    ]
    if omitted_fields:
        summary["payload_truncated"] = True
        summary["omitted_fields"] = omitted_fields
        summary["payload_sha256"] = _payload_digest(
            {
                "job_id": job_id,
                "ok": bool(result.get("ok")),
                "finished_at": result.get("finished_at"),
            }
        )
    return summary


def _delegated_progress(results: List[Dict[str, Any]], expected: int) -> str:
    completed = len(results)
    failed = sum(1 for item in results if not item.get("ok"))
    if completed < expected:
        return f"Background child jobs completed: {completed}/{expected}."
    if failed:
        return f"{failed} background child job(s) failed."
    return f"All {expected} background child job(s) completed."


def _append_task_event(
    task_dir: Path,
    event_type: str,
    payload: Dict[str, Any],
    *,
    workspace_root: Path,
) -> None:
    target = task_dir / EVENTS_FILENAME
    task_dir_fd = _open_private_event_task_dir(task_dir)
    lock_fd: int | None = None
    try:
        lock_path = task_dir / ".events.lock"
        lock_fd = _open_event_path(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=task_dir_fd,
        )
        _require_private_event_fd(lock_fd, label="event lock")
        _acquire_event_lock(lock_fd)
        sequence_path = task_dir / EVENT_SEQUENCE_FILENAME
        sequence_state = _read_event_sequence_state(
            sequence_path,
            dir_fd=task_dir_fd,
        )
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        event_fd = _open_event_path(target, flags, 0o600, dir_fd=task_dir_fd)
        try:
            _require_private_event_fd(
                event_fd,
                label="event log",
                tighten_legacy_permissions=True,
            )
            tail_sequence = _last_complete_event_sequence_from_fd(event_fd)
            if tail_sequence is not None:
                last_sequence = tail_sequence
            elif sequence_state is not None:
                last_sequence = sequence_state
            else:
                last_sequence = _last_event_sequence(target, dir_fd=task_dir_fd)
            if last_sequence >= MAX_EVENT_SEQUENCE:
                raise OverflowError("task event sequence exhausted the int64 range")
            sequence = last_sequence + 1
            redaction = redact_observability_payload(
                payload,
                secrets=collect_observability_secrets(),
                roots=default_observability_roots(workspace_root),
            )
            event = {
                "event_id": f"{task_dir.name}:{sequence}",
                "sequence": sequence,
                "event": event_type,
                "recorded_at": time.time(),
                "data": redaction.value,
                "sanitization": {
                    "redacted_before_persistence": True,
                    "redacted": redaction.replacement_count > 0,
                    "payload_truncated": False,
                },
            }
            encoded_event = _task_event_bytes(event)
            if len(encoded_event) > MAX_TASK_EVENT_BYTES:
                event["data"] = _bounded_event_payload(redaction.value)
                event["sanitization"]["payload_truncated"] = True
                encoded_event = _task_event_bytes(event)
            if len(encoded_event) > MAX_TASK_EVENT_BYTES:
                raise ValueError("bounded task event exceeds the hard size limit")
            _ensure_event_line_boundary(event_fd)
            remaining = memoryview(encoded_event)
            while remaining:
                written = os.write(event_fd, remaining)
                if written <= 0:
                    raise OSError("failed to append task event")
                remaining = remaining[written:]
            # The JSONL is authoritative.  Updating the bounded sidecar only
            # after append lets the next writer recover from a stale cache by
            # reading the last complete line without rescanning the whole log.
            _write_event_sequence_state(
                sequence_path,
                sequence,
                dir_fd=task_dir_fd,
            )
        finally:
            os.close(event_fd)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if task_dir_fd is not None:
            os.close(task_dir_fd)


def _open_event_path(
    path: Path,
    flags: int,
    mode: int = 0o600,
    *,
    dir_fd: int | None = None,
) -> int:
    if dir_fd is None:
        return os.open(path, flags, mode)
    return os.open(path.name, flags, mode, dir_fd=dir_fd)


def _open_private_event_task_dir(task_dir: Path) -> int | None:
    return _open_private_task_dir(task_dir, create=False)


def _acquire_event_lock(fd: int) -> None:
    _acquire_bounded_file_lock(
        fd,
        timeout_seconds=_EVENT_LOCK_TIMEOUT_SECONDS,
        label="task event lock",
    )


def _acquire_bounded_file_lock(
    fd: int,
    *,
    timeout_seconds: float,
    label: str,
) -> None:
    if os.name != "posix":
        return
    import fcntl

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{label} acquisition timed out")
            time.sleep(0.005)


def _last_complete_event_sequence_from_fd(fd: int) -> Optional[int]:
    size = os.fstat(fd).st_size
    if size == 0:
        return 0
    read_size = min(size, MAX_TASK_EVENT_BYTES * 4)
    os.lseek(fd, size - read_size, os.SEEK_SET)
    raw = os.read(fd, read_size)
    if size > read_size:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    for line in reversed(raw.splitlines()):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        sequence = event.get("sequence")
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and 0 <= sequence <= MAX_EVENT_SEQUENCE
        ):
            return sequence
    return None


def _ensure_event_line_boundary(fd: int) -> None:
    size = os.fstat(fd).st_size
    if size <= 0:
        return
    os.lseek(fd, -1, os.SEEK_END)
    if os.read(fd, 1) != b"\n":
        os.write(fd, b"\n")


def _read_event_sequence_state(
    path: Path,
    *,
    dir_fd: int | None = None,
) -> Optional[int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = _open_event_path(path, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise OSError("event sequence state must be a single-link regular file")
        if os.name == "posix" and current.st_uid != os.geteuid():
            raise OSError("event sequence state has an unexpected owner")
        if stat.S_IMODE(current.st_mode) & 0o077:
            raise OSError("event sequence state has unsafe permissions")
        raw_bytes = os.read(fd, _MAX_EVENT_SEQUENCE_STATE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw_bytes) > _MAX_EVENT_SEQUENCE_STATE_BYTES:
        return None
    try:
        raw_text = raw_bytes.decode("ascii", errors="strict")
        if not raw_text or not raw_text.isdecimal():
            return None
        value = int(raw_text)
    except (UnicodeDecodeError, ValueError):
        return None
    if raw_text != str(value):
        return None
    return value if 0 <= value <= MAX_EVENT_SEQUENCE else None


def _write_event_sequence_state(
    path: Path,
    sequence: int,
    *,
    dir_fd: int | None = None,
) -> None:
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise TypeError("event sequence must be an integer")
    if not 0 <= sequence <= MAX_EVENT_SEQUENCE:
        raise OverflowError("event sequence is outside the int64 range")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = _open_event_path(path, flags, 0o600, dir_fd=dir_fd)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise OSError("event sequence state must be a single-link regular file")
        if os.name == "posix" and current.st_uid != os.geteuid():
            raise OSError("event sequence state has an unexpected owner")
        if stat.S_IMODE(current.st_mode) & 0o077:
            raise OSError("event sequence state has unsafe permissions")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = memoryview(str(sequence).encode("ascii"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("failed to update event sequence state")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def _require_private_event_fd(
    fd: int,
    *,
    label: str,
    tighten_legacy_permissions: bool = False,
) -> None:
    current = os.fstat(fd)
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise OSError(f"{label} must be a single-link regular file")
    if os.name == "posix" and current.st_uid != os.geteuid():
        raise OSError(f"{label} has an unexpected owner")
    unsafe_permissions = stat.S_IMODE(current.st_mode) & 0o077
    if unsafe_permissions:
        # A historical 0644-style log can be made private after inode, owner,
        # and link validation.  Never trust or migrate a file that another
        # user could write: it may still be held open after chmod.
        if unsafe_permissions & 0o022:
            raise OSError(f"{label} has unsafe writable permissions")
        if not tighten_legacy_permissions or not hasattr(os, "fchmod"):
            raise OSError(f"{label} has unsafe permissions")
        # Historical v2 event logs were commonly created as 0644.  Tighten only
        # after the open descriptor has passed type, ownership, and link-count
        # validation so a symlink/hardlink cannot turn migration into a chmod
        # gadget.  Re-check the descriptor after mutation before appending.
        os.fchmod(fd, 0o600)
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (os.name == "posix" and current.st_uid != os.geteuid())
            or stat.S_IMODE(current.st_mode) & 0o077
        ):
            raise OSError(f"{label} could not be made private")


def _last_event_sequence(path: Path, *, dir_fd: int | None = None) -> int:
    last = 0
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = _open_event_path(path, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return 0
    try:
        _require_private_event_fd(
            fd,
            label="event log",
            tighten_legacy_permissions=True,
        )
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            fd = -1
            while True:
                line = handle.readline(MAX_TASK_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_TASK_EVENT_BYTES and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = handle.readline(MAX_TASK_EVENT_BYTES + 1)
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(event, dict):
                    value = event.get("sequence")
                    if (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and last < value <= MAX_EVENT_SEQUENCE
                    ):
                        last = value
    except OSError:
        return 0
    finally:
        if fd >= 0:
            os.close(fd)
    return last


def _task_tool_summaries(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for item in tools[-MAX_TASK_TOOL_SUMMARIES:]:
        summaries.append(
            {
                "name": _bounded_text(item.get("name"), 256),
                "kind": _bounded_text(item.get("kind"), 64),
                "status": _bounded_text(item.get("status"), 64),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "elapsed_s": item.get("elapsed_s"),
                "summary": _bounded_text(item.get("summary"), 2000),
                "error": (
                    _bounded_text(item.get("error"), 2000)
                    if item.get("error") is not None
                    else None
                ),
                "span_id": (
                    _bounded_text(item.get("span_id"), 256)
                    if item.get("span_id") is not None
                    else None
                ),
                "parent_span_id": (
                    _bounded_text(item.get("parent_span_id"), 256)
                    if item.get("parent_span_id") is not None
                    else None
                ),
                "depth": item.get("depth"),
            }
        )
    return summaries


def _task_event_bytes(event: Dict[str, Any]) -> bytes:
    return (
        json.dumps(
            event,
            ensure_ascii=False,
            default=str,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _bounded_event_payload(value: Any) -> Dict[str, Any]:
    canonical = _json_bytes(value)
    retained: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key in (
            "name",
            "kind",
            "status",
            "ok",
            "trace_id",
            "span_id",
            "parent_span_id",
            "step_id",
            "job_id",
            "depth",
            "iteration",
            "model",
            "finish_reason",
            "started_at",
            "finished_at",
            "recorded_at",
        ):
            raw = value.get(key)
            if raw is None or isinstance(raw, (bool, int, float)):
                retained[key] = raw
            elif isinstance(raw, str):
                retained[key] = _bounded_text(raw, 512)
        for key in ("summary", "error", "message"):
            if key in value:
                retained[key] = _bounded_text(value.get(key), 2000)
    retained.update(
        {
            "payload_truncated": True,
            "original_bytes": len(canonical),
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "top_level_item_count": len(value) if isinstance(value, (dict, list)) else 1,
        }
    )
    return retained


def _task_step_summaries(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for item in steps[-MAX_TASK_STEP_SUMMARIES:]:
        summaries.append(
            {
                "step_id": _bounded_text(item.get("step_id"), 256),
                "type": _bounded_text(item.get("type"), 64),
                "parent_step_id": (
                    _bounded_text(item.get("parent_step_id"), 256)
                    if item.get("parent_step_id") is not None
                    else None
                ),
                "depth": item.get("depth"),
                "status": _bounded_text(item.get("status"), 64),
                "title": _bounded_text(item.get("title"), 512),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "elapsed_s": item.get("elapsed_s"),
                "summary": _bounded_text(item.get("summary"), 2000),
                "error": (
                    _bounded_text(item.get("error"), 2000)
                    if item.get("error") is not None
                    else None
                ),
                "metadata": _bounded_mapping(item.get("metadata")),
                "estimated_usage": normalize_usage(item.get("estimated_usage")),
                "actual_usage": normalize_usage(item.get("actual_usage")),
                "inclusive_usage": normalize_usage(item.get("inclusive_usage")),
                "raw_event_types": [
                    _bounded_text(value, 128)
                    for value in list(item.get("raw_event_types") or [])[:20]
                ],
            }
        )
    return summaries


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def _message_manifest(messages: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in list(messages or [])[:200]:
        item = raw if isinstance(raw, dict) else {"content": raw}
        content = item.get("content")
        serialized = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        serialized_message = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        reported_content_chars = len(serialized)
        if isinstance(content, str):
            for match in _TRUNCATED_ORIGINAL_CHARS_RE.finditer(content):
                try:
                    reported_content_chars = max(
                        reported_content_chars,
                        int(match.group(1)),
                    )
                except ValueError:
                    continue
        reported_message_chars = len(serialized_message) + max(
            0,
            reported_content_chars - len(serialized),
        )
        out.append(
            {
                "role": _bounded_text(item.get("role"), 256),
                "name": _bounded_text(item.get("name"), 256),
                "char_count": reported_message_chars,
                "message_sha256": hashlib.sha256(serialized_message.encode("utf-8")).hexdigest(),
                "content_char_count": reported_content_chars,
                "content_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "content_preview": serialized[:1024],
            }
        )
    return out


def _tool_schema_manifest(schemas: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in list(schemas or [])[:200]:
        item = raw if isinstance(raw, dict) else {"schema": raw}
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = function.get("name") or item.get("name") or ""
        serialized = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        out.append(
            {
                "name": _bounded_text(name, 256),
                "char_count": len(serialized),
                "schema_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            }
        )
    return out


def _truncated_context_payload(
    payload: Dict[str, Any],
    *,
    original_bytes: int,
    content_sha256: str,
) -> Dict[str, Any]:
    bounded: Dict[str, Any] = {
        "schema_version": payload.get("schema_version", 1),
        "task_id": _bounded_text(payload.get("task_id"), 256),
        "snapshot_id": _bounded_text(payload.get("snapshot_id"), 256),
        "captured_at": payload.get("captured_at"),
        "backend": _bounded_text(payload.get("backend"), 256),
        "model": _bounded_text(payload.get("model"), 1024),
        "iteration": payload.get("iteration"),
        "coverage": _bounded_text(payload.get("coverage"), 256),
        "omitted": [_bounded_text(item, 512) for item in list(payload.get("omitted") or [])[:200]],
        "context_kind": _bounded_text(payload.get("context_kind"), 256),
        "trace_id": _bounded_text(payload.get("trace_id"), 256),
        "span_id": _bounded_text(payload.get("span_id"), 256),
        "parent_span_id": _bounded_text(payload.get("parent_span_id"), 256),
        "depth": payload.get("depth"),
        "estimated_tokens": payload.get("estimated_tokens"),
        "model_selection": _bounded_mapping(payload.get("model_selection")),
        "session_messages": _message_manifest(payload.get("session_messages")),
        "effective_messages": _message_manifest(payload.get("effective_messages")),
        "tool_schemas": _tool_schema_manifest(payload.get("tool_schemas")),
        "resources": _resource_manifest(payload.get("resources")),
        "capture_status": "truncated",
        "truncated": True,
        "original_bytes": original_bytes,
        "content_sha256": content_sha256,
        "sanitization": payload.get("sanitization"),
    }
    _set_stored_bytes(bounded)
    if len(_json_bytes(bounded)) > MAX_CONTEXT_ARTIFACT_BYTES:
        for key in ("session_messages", "effective_messages"):
            for item in bounded.get(key) or []:
                if isinstance(item, dict):
                    item.pop("content_preview", None)
        _set_stored_bytes(bounded)
    if len(_json_bytes(bounded)) > MAX_CONTEXT_ARTIFACT_BYTES:
        bounded["session_messages"] = []
        bounded["effective_messages"] = []
        bounded["tool_schemas"] = []
        bounded["resources"] = []
        bounded["truncation_reason"] = "artifact_size_limit"
        _set_stored_bytes(bounded)
    if len(_json_bytes(bounded)) > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("truncated context artifact exceeds the hard size limit")
    return bounded


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _bounded_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:100]:
        key = _bounded_text(raw_key, 256)
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            out[key] = raw_value
        else:
            out[key] = _bounded_text(raw_value, 1024)
    return out


def _resource_manifest(resources: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in list(resources or [])[:200]:
        item = raw if isinstance(raw, dict) else {}
        out.append(
            {
                "sequence": item.get("sequence"),
                "media_type": _bounded_text(item.get("media_type"), 256),
                "size_bytes": item.get("size_bytes"),
                "sha256": _bounded_text(item.get("sha256"), 128),
            }
        )
    return out


def _set_stored_bytes(payload: Dict[str, Any]) -> None:
    for _ in range(4):
        stored_bytes = len(_json_bytes(payload))
        if payload.get("stored_bytes") == stored_bytes:
            return
        payload["stored_bytes"] = stored_bytes


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _require_private_path(path: Path, *, mode: int, directory: bool) -> None:
    current = path.lstat()
    if stat.S_ISLNK(current.st_mode):
        raise OSError("private observability path must not be a symlink")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(current.st_mode):
        raise OSError("private observability path has an invalid inode type")
    if stat.S_IMODE(current.st_mode) != mode:
        raise OSError("private observability path has unsafe permissions")
    if os.name == "posix" and current.st_uid != os.geteuid():
        raise OSError("private observability path has an unexpected owner")
    if not directory and current.st_nlink != 1:
        raise OSError("private observability artifact must have one hard link")


def _normalize_usage_payload(usage: Dict[str, Any]) -> Dict[str, int]:
    normalized = normalize_usage(usage)
    return {
        key: normalized[key]
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    }


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
