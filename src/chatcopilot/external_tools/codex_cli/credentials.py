"""Private, lane-scoped Codex credential storage and refresh leases."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, Literal, Mapping, cast

CredentialLane = Literal["main", "worker"]
CredentialState = Literal["missing", "recognized", "ready", "invalid", "busy"]

_AUTH_FILE = "auth.json"
_METADATA_FILE = "credential.json"
_LOCK_DIRECTORY = ".locks"
_MAX_CREDENTIAL_BYTES = 4 * 1024 * 1024
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_METADATA_KEYS = {
    "schema_version",
    "generation",
    "installed_at",
    "refreshed_at",
    "last_error_code",
}


class CredentialError(RuntimeError):
    """A stable, non-secret credential storage failure."""

    def __init__(self, code: str) -> None:
        if not _SAFE_ERROR_CODE.fullmatch(code):
            raise ValueError("credential error code must be stable and non-secret")
        self.code = code
        super().__init__(code)


class CredentialBusyError(CredentialError):
    """Raised when another process holds the selected lane."""

    def __init__(self) -> None:
        super().__init__("lock_busy")


@dataclass(frozen=True)
class CredentialStatus:
    """Safe status data suitable for operator output."""

    lane: CredentialLane
    state: CredentialState
    generation: int | None = None
    credential_updated_at: str | None = None
    installed_at: str | None = None
    refreshed_at: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "state": self.state,
            "credential_updated_at": self.credential_updated_at,
            "installed_at": self.installed_at,
            "refreshed_at": self.refreshed_at,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class CredentialLease:
    """The credential generation copied into one isolated Codex runtime."""

    lane: CredentialLane
    generation: int
    runtime_home: Path


@dataclass
class LaneCredentialLock:
    """An acquired lane lock, reusable by an interactive login installer."""

    auth_root: Path
    lane: CredentialLane
    _handle: BinaryIO

    @property
    def active(self) -> bool:
        return not self._handle.closed


def authoritative_home(auth_root: Path, lane: CredentialLane) -> Path:
    """Return the fixed authority directory for a lane without resolving symlinks."""

    root = _absolute_path(auth_root)
    checked_lane = _validate_lane(lane)
    return root if checked_lane == "main" else root / "worker"


def validate_auth_root_path(auth_root: str | Path) -> Path:
    """Reject relative, personal, and desktop Codex authority roots."""

    raw = str(auth_root).strip()
    if not raw:
        raise CredentialError("auth_root_missing_config")
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        raise CredentialError("auth_root_not_absolute")
    normalized = _absolute_path(expanded)
    candidates = _path_identities(normalized)
    forbidden_roots = _path_identities(Path.home() / ".codex")
    for env_name in ("CODEX_HOME", "CHATCOPILOT_CODEX_HOME"):
        personal_raw = os.environ.get(env_name, "").strip()
        if not personal_raw:
            continue
        personal = Path(os.path.expanduser(personal_raw))
        if personal.is_absolute():
            forbidden_roots.update(_path_identities(personal))
    for candidate in candidates:
        if any(
            candidate == forbidden or candidate.is_relative_to(forbidden)
            for forbidden in forbidden_roots
        ):
            raise CredentialError("auth_root_personal_forbidden")
        lexical = os.path.normpath(str(candidate)).casefold()
        if re.match(r"^/mnt/[^/]+/users/[^/]+/\.codex(?:/|$)", lexical):
            raise CredentialError("auth_root_personal_forbidden")
    return normalized


def authoritative_auth_path(auth_root: Path, lane: CredentialLane) -> Path:
    return authoritative_home(auth_root, lane) / _AUTH_FILE


@contextlib.contextmanager
def credential_lock(
    auth_root: Path,
    lane: CredentialLane,
    *,
    blocking: bool = True,
    create: bool = True,
) -> Iterator[LaneCredentialLock]:
    """Acquire one lane lock.

    Interactive login holds this lock before starting device authorization and
    passes the yielded object to :func:`install_login_credential`.
    """

    root = _absolute_path(auth_root)
    checked_lane = _validate_lane(lane)
    handle = _acquire_lock(root, checked_lane, blocking=blocking, create=create)
    lock = LaneCredentialLock(root, checked_lane, handle)
    try:
        yield lock
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextlib.contextmanager
def credential_lease(
    auth_root: Path,
    lane: CredentialLane,
    runtime_home: Path,
    *,
    blocking: bool = True,
) -> Iterator[CredentialLease]:
    """Copy a lane credential into a runtime and persist a valid refresh.

    Copy-back runs for normal return and for every exceptional exit, including
    cancellation. An invalid runtime credential never replaces the authority.
    """

    root = _absolute_path(auth_root)
    checked_lane = _validate_lane(lane)
    runtime = _absolute_path(runtime_home)
    body_completed = False
    with credential_lock(root, checked_lane, blocking=blocking, create=False):
        authority_home = authoritative_home(root, checked_lane)
        _validate_private_directory(authority_home, "authority_home")
        authority_auth = authority_home / _AUTH_FILE
        authority_metadata = authority_home / _METADATA_FILE
        source = _read_credential(authority_auth)
        metadata = _read_metadata(authority_metadata)
        generation = cast(int, metadata["generation"]) if metadata is not None else 0

        _ensure_private_directory(runtime, create=True, code_prefix="runtime_home")
        runtime_auth = runtime / _AUTH_FILE
        _reject_symlink_if_present(runtime_auth, "runtime_auth_symlink")
        _atomic_write(runtime_auth, source)

        try:
            yield CredentialLease(checked_lane, generation, runtime)
            body_completed = True
        finally:
            try:
                refreshed = _read_credential(runtime_auth, code_prefix="runtime_auth")
                if refreshed != source:
                    _atomic_write(authority_auth, refreshed)
                    _write_refresh_metadata(authority_metadata, metadata)
            except CredentialError as exc:
                _record_error(authority_metadata, metadata, exc.code)
                if body_completed or sys.exc_info()[0] is None:
                    raise


def install_login_credential(
    auth_root: Path,
    lane: CredentialLane,
    staging_home: Path,
    *,
    held_lock: LaneCredentialLock | None = None,
    installed_at: str | None = None,
) -> int:
    """Atomically install a successful device-login credential.

    A successful explicit install increments only the selected lane's
    generation. The staging home and credential must already be private.
    """

    staged_auth = load_staged_login_credential(staging_home)
    return install_login_credential_data(
        auth_root,
        lane,
        staged_auth,
        held_lock=held_lock,
        installed_at=installed_at,
    )


def load_staged_login_credential(staging_home: Path) -> bytes:
    """Read and validate one private staged ChatGPT credential."""

    staging = _absolute_path(staging_home)
    _validate_private_directory(staging, "staging_home")
    return _read_credential(staging / _AUTH_FILE, code_prefix="staging_auth")


def install_login_credential_data(
    auth_root: Path,
    lane: CredentialLane,
    staged_auth: bytes,
    *,
    held_lock: LaneCredentialLock | None = None,
    installed_at: str | None = None,
) -> int:
    """Install already validated staged bytes after staging cleanup succeeds."""

    root = _absolute_path(auth_root)
    checked_lane = _validate_lane(lane)
    staged_auth = _validate_credential_payload(
        bytes(staged_auth),
        code_prefix="staging_auth",
    )
    if held_lock is None:
        with credential_lock(root, checked_lane, blocking=False, create=True) as lock:
            return install_login_credential_data(
                root,
                checked_lane,
                staged_auth,
                held_lock=lock,
                installed_at=installed_at,
            )

    _validate_held_lock(held_lock, root, checked_lane)
    authority_home = authoritative_home(root, checked_lane)
    _ensure_private_directory(authority_home, create=True, code_prefix="authority_home")
    authority_auth = authority_home / _AUTH_FILE
    authority_metadata = authority_home / _METADATA_FILE
    _reject_symlink_if_present(authority_auth, "auth_symlink")
    previous_auth = (
        _read_auth_rollback_snapshot(authority_auth)
        if os.path.lexists(authority_auth)
        else None
    )
    metadata = _read_metadata(authority_metadata)
    generation = (cast(int, metadata["generation"]) if metadata is not None else 0) + 1
    timestamp = installed_at or _utc_now()
    new_metadata = {
        "schema_version": 1,
        "generation": generation,
        "installed_at": _validate_timestamp(timestamp),
        "refreshed_at": None,
        "last_error_code": None,
    }

    # Generation becomes visible before the credential. All cooperating
    # readers hold the same lock, and a process crash therefore fails
    # conservatively by invalidating stale resume IDs. Ordinary write failures
    # restore the credential before restoring its generation metadata.
    auth_write_attempted = False
    try:
        _atomic_write_json(authority_metadata, new_metadata)
        auth_write_attempted = True
        _atomic_write(authority_auth, staged_auth)
    except BaseException:
        if auth_write_attempted:
            try:
                if previous_auth is None:
                    _remove_private_file(authority_auth)
                else:
                    _atomic_write(authority_auth, previous_auth)
            except CredentialError as rollback_error:
                # Keep the new generation metadata when credential identity
                # restoration is uncertain. This invalidates stale resumes
                # instead of pairing them with an unknown credential.
                raise CredentialError(
                    "credential_install_rollback_failed"
                ) from rollback_error
        try:
            if metadata is None:
                _remove_private_file(authority_metadata)
            else:
                _atomic_write_json(authority_metadata, metadata)
        except CredentialError as rollback_error:
            raise CredentialError("credential_install_rollback_failed") from rollback_error
        raise
    return generation


def credential_status(auth_root: Path, lane: CredentialLane) -> CredentialStatus:
    """Return non-secret lane status without waiting for an active invocation."""

    root = _absolute_path(auth_root)
    checked_lane = _validate_lane(lane)
    if not os.path.lexists(root):
        return CredentialStatus(checked_lane, "missing", error_code="auth_missing")
    try:
        _validate_private_directory(root, "auth_root")
        with credential_lock(root, checked_lane, blocking=False, create=True):
            return _read_status_snapshot(root, checked_lane)
    except CredentialBusyError:
        return CredentialStatus(checked_lane, "busy", error_code="lock_busy")
    except CredentialError as exc:
        return CredentialStatus(checked_lane, "invalid", error_code=exc.code)


def _read_status_snapshot(
    root: Path,
    lane: CredentialLane,
) -> CredentialStatus:
    authority_home = authoritative_home(root, lane)
    if os.path.lexists(authority_home):
        _validate_private_directory(authority_home, "authority_home")
    auth_path = authority_home / _AUTH_FILE
    if not os.path.lexists(auth_path):
        return CredentialStatus(lane, "missing", error_code="auth_missing")
    credential = _read_credential(auth_path)
    del credential
    metadata = _read_metadata(auth_path.parent / _METADATA_FILE)
    updated_at = _mtime_timestamp(auth_path)
    if metadata is None:
        return CredentialStatus(
            lane,
            "recognized",
            generation=0,
            credential_updated_at=updated_at,
        )
    generation = cast(int, metadata["generation"])
    state: CredentialState = "ready" if generation > 0 else "recognized"
    return CredentialStatus(
        lane,
        state,
        generation=generation,
        credential_updated_at=updated_at,
        installed_at=_optional_str(metadata["installed_at"]),
        refreshed_at=_optional_str(metadata["refreshed_at"]),
        error_code=_optional_str(metadata["last_error_code"]),
    )


def _validate_lane(lane: str) -> CredentialLane:
    if lane not in {"main", "worker"}:
        raise ValueError("lane must be 'main' or 'worker'")
    return lane  # type: ignore[return-value]


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _path_identities(path: Path) -> set[Path]:
    normalized = _absolute_path(path)
    identities = {normalized}
    try:
        identities.add(normalized.resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise CredentialError("auth_root_invalid") from exc
    return identities


def _acquire_lock(
    root: Path,
    lane: CredentialLane,
    *,
    blocking: bool,
    create: bool,
) -> BinaryIO:
    if create:
        _ensure_private_directory(root, create=True, code_prefix="auth_root")
    else:
        _validate_private_directory(root, "auth_root")
    lock_dir = root / _LOCK_DIRECTORY
    _ensure_private_directory(lock_dir, create=True, code_prefix="lock_home")
    lock_path = lock_dir / f"{lane}.lock"
    _reject_symlink_if_present(lock_path, "lock_symlink")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CredentialError("lock_open_failed") from exc
    handle = os.fdopen(fd, "r+b", buffering=0)
    try:
        _validate_file_stat(os.fstat(fd), "lock", require_single_link=True)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, operation)
        except BlockingIOError as exc:
            raise CredentialBusyError() from exc
        return handle
    except BaseException:
        handle.close()
        raise


def _validate_held_lock(
    lock: LaneCredentialLock,
    root: Path,
    lane: CredentialLane,
) -> None:
    if not lock.active or lock.auth_root != root or lock.lane != lane:
        raise CredentialError("lock_mismatch")


def _ensure_private_directory(path: Path, *, create: bool, code_prefix: str) -> None:
    if not os.path.lexists(path):
        if not create:
            raise CredentialError(f"{code_prefix}_missing")
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            # Another lane may have created a shared parent (notably .locks)
            # after the lexists check. The validation below remains the
            # authoritative ownership/type/permission check.
            pass
        except OSError as exc:
            raise CredentialError(f"{code_prefix}_create_failed") from exc
    _validate_private_directory(path, code_prefix)


def _validate_private_directory(path: Path, code_prefix: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CredentialError(f"{code_prefix}_missing") from exc
    except OSError as exc:
        raise CredentialError(f"{code_prefix}_stat_failed") from exc
    if stat.S_ISLNK(info.st_mode):
        raise CredentialError(f"{code_prefix}_symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise CredentialError(f"{code_prefix}_not_directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise CredentialError(f"{code_prefix}_permissions")
    if info.st_uid != os.geteuid():
        raise CredentialError(f"{code_prefix}_owner")


def _reject_symlink_if_present(path: Path, code: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CredentialError("credential_stat_failed") from exc
    if stat.S_ISLNK(info.st_mode):
        raise CredentialError(code)


def _read_credential(path: Path, *, code_prefix: str = "auth") -> bytes:
    raw = _read_private_file(path, code_prefix=code_prefix)
    return _validate_credential_payload(raw, code_prefix=code_prefix)


def _validate_credential_payload(raw: bytes, *, code_prefix: str) -> bytes:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError(f"{code_prefix}_invalid_json") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise CredentialError(f"{code_prefix}_invalid_json")
    if parsed.get("auth_mode") != "chatgpt":
        raise CredentialError(f"{code_prefix}_unsupported_mode")
    api_key = parsed.get("OPENAI_API_KEY")
    if api_key not in {None, ""}:
        raise CredentialError(f"{code_prefix}_api_key_forbidden")
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        raise CredentialError(f"{code_prefix}_unrecognized")
    for token_name in ("access_token", "refresh_token"):
        token = tokens.get(token_name)
        if not isinstance(token, str) or not token:
            raise CredentialError(f"{code_prefix}_unrecognized")
    return raw


def _read_private_file(path: Path, *, code_prefix: str) -> bytes:
    return _read_owned_file(
        path,
        code_prefix=code_prefix,
        require_private_permissions=True,
    )


def _read_auth_rollback_snapshot(path: Path) -> bytes:
    """Read old auth bytes so an explicit login can repair their format/mode."""

    return _read_owned_file(
        path,
        code_prefix="auth",
        require_private_permissions=False,
    )


def _read_owned_file(
    path: Path,
    *,
    code_prefix: str,
    require_private_permissions: bool,
) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise CredentialError(f"{code_prefix}_missing") from exc
    except OSError as exc:
        raise CredentialError(f"{code_prefix}_stat_failed") from exc
    if stat.S_ISLNK(before.st_mode):
        raise CredentialError(f"{code_prefix}_symlink")
    _validate_file_stat(
        before,
        code_prefix,
        require_single_link=True,
        require_private_permissions=require_private_permissions,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CredentialError(f"{code_prefix}_open_failed") from exc
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CredentialError(f"{code_prefix}_changed")
        _validate_file_stat(
            after,
            code_prefix,
            require_single_link=True,
            require_private_permissions=require_private_permissions,
        )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_CREDENTIAL_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_CREDENTIAL_BYTES:
                raise CredentialError(f"{code_prefix}_too_large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _validate_file_stat(
    info: os.stat_result,
    code_prefix: str,
    *,
    require_single_link: bool,
    require_private_permissions: bool = True,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError(f"{code_prefix}_not_regular")
    if (
        require_private_permissions
        and stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise CredentialError(f"{code_prefix}_permissions")
    if info.st_uid != os.geteuid():
        raise CredentialError(f"{code_prefix}_owner")
    if require_single_link and info.st_nlink != 1:
        raise CredentialError(f"{code_prefix}_hardlinked")


def _read_metadata(path: Path) -> dict[str, object] | None:
    if not os.path.lexists(path):
        return None
    raw = _read_private_file(path, code_prefix="metadata")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError("metadata_invalid_json") from exc
    if not isinstance(parsed, dict) or set(parsed) != _METADATA_KEYS:
        raise CredentialError("metadata_invalid")
    if parsed.get("schema_version") != 1:
        raise CredentialError("metadata_invalid")
    generation = parsed.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise CredentialError("metadata_invalid")
    _validate_optional_timestamp(parsed.get("installed_at"))
    _validate_optional_timestamp(parsed.get("refreshed_at"))
    error_code = parsed.get("last_error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(error_code)
    ):
        raise CredentialError("metadata_invalid")
    return parsed


def _write_refresh_metadata(
    path: Path,
    metadata: Mapping[str, object] | None,
) -> None:
    current = _metadata_or_default(metadata)
    current["refreshed_at"] = _utc_now()
    current["last_error_code"] = None
    _atomic_write_json(path, current)


def _record_error(
    path: Path,
    metadata: Mapping[str, object] | None,
    error_code: str,
) -> None:
    try:
        current = _metadata_or_default(metadata)
        current["last_error_code"] = error_code
        _atomic_write_json(path, current)
    except CredentialError:
        return


def _metadata_or_default(metadata: Mapping[str, object] | None) -> dict[str, object]:
    if metadata is not None:
        return dict(metadata)
    return {
        "schema_version": 1,
        "generation": 0,
        "installed_at": None,
        "refreshed_at": None,
        "last_error_code": None,
    }


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, encoded)


def _atomic_write(path: Path, payload: bytes) -> None:
    _validate_private_directory(path.parent, "authority_home")
    _reject_symlink_if_present(path, "credential_symlink")
    suffix = f".tmp-{os.getpid()}-{os.urandom(8).hex()}"
    temporary = path.with_name(path.name + suffix)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise CredentialError("credential_write_failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_private_file(path: Path) -> None:
    _reject_symlink_if_present(path, "credential_symlink")
    try:
        path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CredentialError("credential_write_failed") from exc


def _mtime_timestamp(path: Path) -> str:
    try:
        modified_at = path.stat().st_mtime
        return datetime.fromtimestamp(modified_at, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError) as exc:
        raise CredentialError("auth_stat_failed") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CredentialError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise CredentialError("timestamp_invalid")
    return value


def _validate_optional_timestamp(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise CredentialError("metadata_invalid")
    try:
        _validate_timestamp(value)
    except CredentialError as exc:
        raise CredentialError("metadata_invalid") from exc


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "CredentialBusyError",
    "CredentialError",
    "CredentialLane",
    "CredentialLease",
    "CredentialState",
    "CredentialStatus",
    "LaneCredentialLock",
    "authoritative_auth_path",
    "authoritative_home",
    "credential_lease",
    "credential_lock",
    "credential_status",
    "install_login_credential",
    "install_login_credential_data",
    "load_staged_login_credential",
    "validate_auth_root_path",
]
