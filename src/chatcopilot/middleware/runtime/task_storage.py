"""Private task files, completion locks and durable event sequence ownership."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Literal, Optional

from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.jobs import read_json_file, write_json_atomic
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets,
    default_observability_roots,
    load_bounded_observability_json,
    redact_observability_payload,
)
from chatcopilot.core.workspace_runtime import Workspace

from . import task_projection as _projection


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
    actors_root = state_root / _projection.GROUP_TASK_ACTORS_DIRNAME
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
    intake_root = state_root / _projection.GROUP_TASK_INTAKE_DIRNAME
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
    if tasks_dir.name != _projection.TASKS_DIRNAME or not _projection._TASK_ID_RE.fullmatch(task_id):
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
            _projection.TASKS_DIRNAME,
            create=create,
        )
        return _open_private_child_dir_at(tasks_fd, task_id, create=create)
    finally:
        if tasks_fd is not None:
            os.close(tasks_fd)
        os.close(root_fd)


def _write_private_json_at(dir_fd: int, name: str, payload: Dict[str, Any]) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise OSError("private artifact name is invalid")
    encoded = _projection._private_json_bytes(payload)
    if len(encoded) > _projection.MAX_CONTEXT_ARTIFACT_BYTES:
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
    max_bytes: int = _projection.MAX_TASK_SUMMARY_BYTES,
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
    bounded_payload = _projection._bounded_task_or_turn_document(name, payload)
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


@contextmanager
def _task_completion_lock(task_dir: Path, *, create: bool = False) -> Iterator[None]:
    task_dir_fd = _open_private_task_dir(task_dir, create=create)
    lock_fd: int | None = None
    try:
        lock_path = task_dir / _projection.COMPLETION_LOCK_FILENAME
        lock_fd = _open_event_path(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=task_dir_fd,
        )
        _require_private_event_fd(lock_fd, label="task completion lock")
        _acquire_bounded_file_lock(
            lock_fd,
            timeout_seconds=_projection._COMPLETION_LOCK_TIMEOUT_SECONDS,
            label="task completion lock",
        )
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if task_dir_fd is not None:
            os.close(task_dir_fd)


def _append_task_event(
    task_dir: Path,
    event_type: str,
    payload: Dict[str, Any],
    *,
    workspace_root: Path,
) -> None:
    target = task_dir / _projection.EVENTS_FILENAME
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
        sequence_path = task_dir / _projection.EVENT_SEQUENCE_FILENAME
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
            if last_sequence >= _projection.MAX_EVENT_SEQUENCE:
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
            encoded_event = _projection._task_event_bytes(event)
            if len(encoded_event) > _projection.MAX_TASK_EVENT_BYTES:
                event["data"] = _projection._bounded_event_payload(redaction.value)
                event["sanitization"]["payload_truncated"] = True
                encoded_event = _projection._task_event_bytes(event)
            if len(encoded_event) > _projection.MAX_TASK_EVENT_BYTES:
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
        timeout_seconds=_projection._EVENT_LOCK_TIMEOUT_SECONDS,
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
    read_size = min(size, _projection.MAX_TASK_EVENT_BYTES * 4)
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
            and 0 <= sequence <= _projection.MAX_EVENT_SEQUENCE
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
        raw_bytes = os.read(fd, _projection._MAX_EVENT_SEQUENCE_STATE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw_bytes) > _projection._MAX_EVENT_SEQUENCE_STATE_BYTES:
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
    return value if 0 <= value <= _projection.MAX_EVENT_SEQUENCE else None


def _write_event_sequence_state(
    path: Path,
    sequence: int,
    *,
    dir_fd: int | None = None,
) -> None:
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise TypeError("event sequence must be an integer")
    if not 0 <= sequence <= _projection.MAX_EVENT_SEQUENCE:
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
                line = handle.readline(_projection.MAX_TASK_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > _projection.MAX_TASK_EVENT_BYTES and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = handle.readline(_projection.MAX_TASK_EVENT_BYTES + 1)
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
                        and last < value <= _projection.MAX_EVENT_SEQUENCE
                    ):
                        last = value
    except OSError:
        return 0
    finally:
        if fd >= 0:
            os.close(fd)
    return last


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


_MAX_HISTORY_TASK_BYTES = 8 * 1024 * 1024


def load_task_history(root: Path | None) -> list[dict[str, Any]]:
    if root is None or not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in root.glob("**/tasks/*/task.json"):
        try:
            if path.stat().st_size > _MAX_HISTORY_TASK_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            continue
        records.append(payload)
    records.sort(
        key=lambda item: _safe_history_timestamp(
            item.get("finished_at") or item.get("updated_at") or 0
        ),
        reverse=True,
    )
    return records


def _safe_history_timestamp(value: Any) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp >= 0 else 0.0


def write_task_artifact(task_dir: Path, *, collection: Literal["contexts", "subagents"],
                        name: str, payload: Dict[str, Any]) -> Path:
    """Write an already-sanitized artifact below a private task directory."""
    for segment in (collection, name):
        if not segment or Path(segment).name != segment or segment in {".", ".."}:
            raise OSError("private artifact path segment is invalid")
    target_dir = task_dir / collection
    target = target_dir / name
    task_fd = _open_private_task_dir(task_dir, create=False)
    collection_fd: int | None = None
    try:
        if task_fd is None:  # pragma: no cover - native Windows validation required
            if target_dir.is_symlink():
                raise OSError("task artifact directory must not be a symlink")
            target_dir.mkdir(mode=0o700, exist_ok=True)
            _chmod_private(target_dir, 0o700)
            _require_private_path(target_dir, mode=0o700, directory=True)
            write_json_atomic(target, payload)
            _chmod_private(target, 0o600)
            _require_private_path(target, mode=0o600, directory=False)
        else:
            collection_fd = _open_private_child_dir_at(task_fd, collection, create=True)
            _write_private_json_at(collection_fd, name, payload)
    finally:
        if collection_fd is not None:
            os.close(collection_fd)
        if task_fd is not None:
            os.close(task_fd)
    return target
