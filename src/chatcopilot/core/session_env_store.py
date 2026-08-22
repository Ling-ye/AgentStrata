"""Secure cross-process session identity and transport-attestation store."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from chatcopilot.contracts.session_attestation import (
    SessionAttestationConsumeResult,
    SessionAttestationResultKind,
)


SESSION_ENV_SCHEMA_VERSION = 2
SESSION_ENV_IDENTITY_KEYS = (
    "CHATCOPILOT_USER_ID",
    "CHATCOPILOT_CHAT_ID",
    "CHATCOPILOT_CHAT_KIND",
    "CHATCOPILOT_USER_NAME",
)
SESSION_ENV_TRANSPORT_KEYS = (
    "CHATCOPILOT_TRANSPORT_HOOK_EVENT",
    "CHATCOPILOT_TRANSPORT_USER_ID",
    "CHATCOPILOT_TRANSPORT_CONTENT_SHA256",
)
SESSION_ENV_ALLOWED_KEYS = frozenset((*SESSION_ENV_IDENTITY_KEYS, *SESSION_ENV_TRANSPORT_KEYS))
MAX_SESSION_ENV_BYTES = 64 * 1024
MAX_SESSION_ATTESTATIONS = 128
# 128 serialized turns at the longest checked-in 6h timeout need 32 days.
SESSION_ATTESTATION_TTL_NS = 45 * 24 * 60 * 60 * 1_000_000_000
SESSION_ATTESTATION_FUTURE_SKEW_NS = 5 * 60 * 1_000_000_000

_ATTESTATION_KEYS = frozenset(
    {
        "record_id",
        "event",
        "transport_user_id",
        "content_sha256",
        "created_at_ns",
    }
)
_SESSION_ENV_FILENAME_RE = re.compile(r"\Acc-sess-(?P<digest>[0-9a-f]{64})\.env\Z")


class SessionEnvSecurityError(RuntimeError):
    """The private session handoff failed a filesystem or schema invariant."""


def session_key_digest(session_key: str) -> str:
    if not session_key:
        raise SessionEnvSecurityError("session key is empty")
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


def session_env_filename(session_key: str) -> str:
    return f"cc-sess-{session_key_digest(session_key)}.env"


def session_env_lock_filename(session_key: str) -> str:
    return f"cc-sess-{session_key_digest(session_key)}.lock"


def normalized_session_env_dir(raw: str | Path) -> Path:
    directory = Path(raw).expanduser()
    if not directory.is_absolute() or ".." in directory.parts:
        raise SessionEnvSecurityError("session env directory must be absolute")
    return directory


def session_env_path(directory: str | Path, session_key: str) -> Path:
    return normalized_session_env_dir(directory) / session_env_filename(session_key)


def session_env_path_from_environment(
    session_key: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    values = os.environ if environ is None else environ
    resolved_key = (
        session_key
        if session_key is not None
        else (values.get("CC_SESSION_KEY") or values.get("CC_HOOK_SESSION_KEY") or "")
    )
    if not resolved_key:
        return None
    raw_directory = (
        values.get("CHATCOPILOT_SESSION_ENV_DIR")
        or (
            f"{values.get('CHATCOPILOT_CC_HOME', '').rstrip('/')}/session-env"
            if values.get("CHATCOPILOT_CC_HOME", "").strip()
            else ""
        )
    ).strip()
    if not raw_directory:
        return None
    try:
        return session_env_path(raw_directory, resolved_key)
    except SessionEnvSecurityError:
        return None


def session_env_digest_from_path(path: Path) -> str:
    match = _SESSION_ENV_FILENAME_RE.fullmatch(path.name)
    if match is None:
        raise SessionEnvSecurityError("session env filename is invalid")
    return match.group("digest")


def session_env_lock_name_from_path(path: Path) -> str:
    return f"cc-sess-{session_env_digest_from_path(path)}.lock"


def _validate_secure_regular_file(file_stat: os.stat_result, *, label: str) -> None:
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or file_stat.st_nlink != 1
        or stat.S_IMODE(file_stat.st_mode) != 0o600
    ):
        raise SessionEnvSecurityError(f"{label} has unsafe ownership, type, links, or mode")


def _open_session_env_directory(path: Path, *, create: bool) -> tuple[Path, int]:
    directory = normalized_session_env_dir(path.parent)
    created = False
    if create:
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            created = True
        except FileExistsError:
            pass
    if created:
        os.chmod(directory, 0o700, follow_symlinks=False)
    try:
        directory_lstat = directory.lstat()
    except OSError as exc:
        raise SessionEnvSecurityError("session env directory is unavailable") from exc
    if stat.S_ISLNK(directory_lstat.st_mode):
        raise SessionEnvSecurityError("session env directory must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        raise SessionEnvSecurityError("session env directory is unavailable") from exc
    directory_stat = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.geteuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
        or (directory_stat.st_dev, directory_stat.st_ino)
        != (directory_lstat.st_dev, directory_lstat.st_ino)
    ):
        os.close(directory_fd)
        raise SessionEnvSecurityError("session env directory has unsafe ownership, type, or mode")
    return directory, directory_fd


@contextmanager
def _locked_session_env(
    path: Path,
    *,
    exclusive: bool,
    create_directory: bool,
    create_lock: bool,
) -> Iterator[tuple[Path, int]]:
    private_dir, dir_fd = _open_session_env_directory(path, create=create_directory)
    lock_name = session_env_lock_name_from_path(path)
    lock_fd: int | None = None
    try:
        flags = os.O_RDWR
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if create_lock:
            try:
                lock_fd = os.open(
                    lock_name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dir_fd,
                )
                os.fchmod(lock_fd, 0o600)
            except FileExistsError:
                lock_fd = os.open(lock_name, flags, dir_fd=dir_fd)
        else:
            lock_fd = os.open(lock_name, flags, dir_fd=dir_fd)
    except OSError as exc:
        os.close(dir_fd)
        raise SessionEnvSecurityError("session env lock is unavailable") from exc
    try:
        lock_stat = os.fstat(lock_fd)
        _validate_secure_regular_file(lock_stat, label="session env lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        lock_path_stat = os.stat(lock_name, dir_fd=dir_fd, follow_symlinks=False)
        _validate_secure_regular_file(lock_path_stat, label="session env lock")
        if (lock_stat.st_dev, lock_stat.st_ino) != (
            lock_path_stat.st_dev,
            lock_path_stat.st_ino,
        ):
            raise SessionEnvSecurityError("session env lock binding changed")
        yield private_dir, dir_fd
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(dir_fd)


def _validate_state(
    payload: object,
    *,
    expected_digest: str,
    max_attestations: int,
) -> dict[str, object]:
    expected_top_keys = {
        "schema_version",
        "session_key_sha256",
        "identity",
        "attestations",
    }
    if not isinstance(payload, dict) or set(payload) != expected_top_keys:
        raise SessionEnvSecurityError("session env schema is invalid")
    if payload.get("schema_version") != SESSION_ENV_SCHEMA_VERSION:
        raise SessionEnvSecurityError("session env schema is invalid")
    if payload.get("session_key_sha256") != expected_digest:
        raise SessionEnvSecurityError("session env session binding is invalid")

    raw_identity = payload.get("identity")
    if not isinstance(raw_identity, dict) or set(raw_identity) != set(SESSION_ENV_IDENTITY_KEYS):
        raise SessionEnvSecurityError("session identity is incomplete")
    identity: dict[str, str] = {}
    for key, value in raw_identity.items():
        if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
            raise SessionEnvSecurityError("session identity value is invalid")
        identity[key] = value

    raw_attestations = payload.get("attestations")
    if not isinstance(raw_attestations, list) or len(raw_attestations) > max_attestations:
        raise SessionEnvSecurityError("session attestation queue is invalid")
    attestations: list[dict[str, object]] = []
    seen_record_ids: set[str] = set()
    for raw_record in raw_attestations:
        if not isinstance(raw_record, dict) or set(raw_record) != _ATTESTATION_KEYS:
            raise SessionEnvSecurityError("session attestation record is invalid")
        record_id = raw_record.get("record_id")
        event = raw_record.get("event")
        transport_user_id = raw_record.get("transport_user_id")
        content_sha256 = raw_record.get("content_sha256")
        created_at_ns = raw_record.get("created_at_ns")
        if (
            not isinstance(record_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", record_id)
            or record_id in seen_record_ids
            or event != "message.received"
            or not isinstance(transport_user_id, str)
            or not transport_user_id
            or "\x00" in transport_user_id
            or len(transport_user_id) > 4096
            or not isinstance(content_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
            or not isinstance(created_at_ns, int)
            or isinstance(created_at_ns, bool)
            or created_at_ns <= 0
        ):
            raise SessionEnvSecurityError("session attestation record is invalid")
        seen_record_ids.add(record_id)
        attestations.append(
            {
                "record_id": record_id,
                "event": event,
                "transport_user_id": transport_user_id,
                "content_sha256": content_sha256,
                "created_at_ns": created_at_ns,
            }
        )
    return {
        "schema_version": SESSION_ENV_SCHEMA_VERSION,
        "session_key_sha256": expected_digest,
        "identity": identity,
        "attestations": attestations,
    }


def _read_state_unlocked(
    path: Path,
    *,
    dir_fd: int,
    allow_missing: bool,
    max_attestations: int,
) -> dict[str, object] | None:
    file_fd: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            file_fd = os.open(path.name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        file_stat = os.fstat(file_fd)
        _validate_secure_regular_file(file_stat, label="session env file")
        if file_stat.st_size > MAX_SESSION_ENV_BYTES:
            raise SessionEnvSecurityError("session env payload is too large")
        chunks: list[bytes] = []
        remaining = MAX_SESSION_ENV_BYTES + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > MAX_SESSION_ENV_BYTES:
            raise SessionEnvSecurityError("session env payload is too large")
    except OSError as exc:
        raise SessionEnvSecurityError("session env file is unavailable") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
    try:
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionEnvSecurityError("session env payload is invalid") from exc
    return _validate_state(
        payload,
        expected_digest=session_env_digest_from_path(path),
        max_attestations=max_attestations,
    )


def _write_state_unlocked(
    path: Path,
    *,
    dir_fd: int,
    state: Mapping[str, object],
    max_attestations: int,
    require_existing: bool,
) -> None:
    validated = _validate_state(
        dict(state),
        expected_digest=session_env_digest_from_path(path),
        max_attestations=max_attestations,
    )
    encoded = (json.dumps(validated, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_SESSION_ENV_BYTES:
        raise SessionEnvSecurityError("session env payload is too large")
    try:
        existing = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if require_existing:
            raise SessionEnvSecurityError("session env target is unavailable") from None
        existing = None
    except OSError as exc:
        raise SessionEnvSecurityError("session env target is unavailable") from exc
    if existing is not None:
        _validate_secure_regular_file(existing, label="session env target")

    temp_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    temp_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        os.fchmod(temp_fd, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(temp_fd, encoded[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except Exception:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def _live_attestations(
    records: list[dict[str, object]],
    *,
    now_ns: int,
    ttl_ns: int,
) -> list[dict[str, object]]:
    cutoff = now_ns - ttl_ns
    live: list[dict[str, object]] = []
    for record in records:
        created_at_ns = int(record["created_at_ns"])
        if created_at_ns > now_ns + SESSION_ATTESTATION_FUTURE_SKEW_NS:
            raise SessionEnvSecurityError("session attestation timestamp is invalid")
        if created_at_ns >= cutoff:
            live.append(record)
    return live


def _validate_write_limits(*, max_attestations: int, ttl_ns: int) -> None:
    if (
        isinstance(max_attestations, bool)
        or not isinstance(max_attestations, int)
        or not 1 <= max_attestations <= MAX_SESSION_ATTESTATIONS
    ):
        raise SessionEnvSecurityError("session attestation queue limit is invalid")
    if (
        isinstance(ttl_ns, bool)
        or not isinstance(ttl_ns, int)
        or not 0 < ttl_ns <= SESSION_ATTESTATION_TTL_NS
    ):
        raise SessionEnvSecurityError("session attestation TTL is invalid")


def write_session_env(
    *,
    directory: str | Path,
    session_key: str,
    values: Mapping[str, str],
    queue_transport: bool = True,
    max_attestations: int = MAX_SESSION_ATTESTATIONS,
    ttl_ns: int = SESSION_ATTESTATION_TTL_NS,
) -> Path:
    """Atomically refresh identity and optionally enqueue one transport record."""

    _validate_write_limits(max_attestations=max_attestations, ttl_ns=ttl_ns)
    if set(values) - SESSION_ENV_ALLOWED_KEYS:
        raise SessionEnvSecurityError("session env contains unsupported keys")
    if any(key not in values for key in SESSION_ENV_IDENTITY_KEYS):
        raise SessionEnvSecurityError("session identity is incomplete")
    present_transport = set(values) & set(SESSION_ENV_TRANSPORT_KEYS)
    if present_transport and present_transport != set(SESSION_ENV_TRANSPORT_KEYS):
        raise SessionEnvSecurityError("transport attestation is incomplete")
    path = session_env_path(directory, session_key)
    with _locked_session_env(
        path,
        exclusive=True,
        create_directory=True,
        create_lock=True,
    ) as (_private_dir, dir_fd):
        state = _read_state_unlocked(
            path,
            dir_fd=dir_fd,
            allow_missing=True,
            max_attestations=max_attestations,
        )
        if state is None:
            state = {
                "schema_version": SESSION_ENV_SCHEMA_VERSION,
                "session_key_sha256": session_key_digest(session_key),
                "identity": {},
                "attestations": [],
            }
        state["identity"] = {key: str(values[key]) for key in SESSION_ENV_IDENTITY_KEYS}
        now_ns = time.time_ns()
        records = _live_attestations(
            list(state["attestations"]),  # type: ignore[arg-type]
            now_ns=now_ns,
            ttl_ns=ttl_ns,
        )
        if present_transport:
            if not queue_transport:
                records = []
            event = values[SESSION_ENV_TRANSPORT_KEYS[0]]
            transport_user_id = values[SESSION_ENV_TRANSPORT_KEYS[1]]
            content_sha256 = values[SESSION_ENV_TRANSPORT_KEYS[2]]
            if (
                event != "message.received"
                or not transport_user_id
                or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
            ):
                raise SessionEnvSecurityError("transport attestation is invalid")
            if len(records) >= max_attestations:
                raise SessionEnvSecurityError("session attestation queue is full")
            records.append(
                {
                    "record_id": secrets.token_hex(16),
                    "event": "message.received",
                    "transport_user_id": transport_user_id,
                    "content_sha256": content_sha256,
                    "created_at_ns": now_ns,
                }
            )
        state["attestations"] = records
        _write_state_unlocked(
            path,
            dir_fd=dir_fd,
            state=state,
            max_attestations=max_attestations,
            require_existing=False,
        )
    return path


def read_session_identity(*, directory: str | Path, session_key: str) -> dict[str, str]:
    return read_session_identity_from_path(session_env_path(directory, session_key))


def read_session_identity_from_path(path: Path) -> dict[str, str]:
    with _locked_session_env(
        path,
        exclusive=False,
        create_directory=False,
        create_lock=False,
    ) as (_private_dir, dir_fd):
        state = _read_state_unlocked(
            path,
            dir_fd=dir_fd,
            allow_missing=False,
            max_attestations=MAX_SESSION_ATTESTATIONS,
        )
    if state is None:
        raise SessionEnvSecurityError("session env file is unavailable")
    return dict(state["identity"])  # type: ignore[arg-type]


def consume_session_attestation(
    path: Path,
    *,
    transport_user_id: str,
    content_sha256: str,
) -> SessionAttestationConsumeResult:
    """Match and consume exactly one actor/body record, preserving mismatches."""

    try:
        path.lstat()
    except FileNotFoundError:
        return SessionAttestationConsumeResult(SessionAttestationResultKind.MISSING)
    except OSError as exc:
        raise SessionEnvSecurityError("session env file is unavailable") from exc
    with _locked_session_env(
        path,
        exclusive=True,
        create_directory=False,
        create_lock=False,
    ) as (_private_dir, dir_fd):
        state = _read_state_unlocked(
            path,
            dir_fd=dir_fd,
            allow_missing=False,
            max_attestations=MAX_SESSION_ATTESTATIONS,
        )
        if state is None:
            return SessionAttestationConsumeResult(SessionAttestationResultKind.MISSING)
        queued = list(state["attestations"])  # type: ignore[arg-type]
        records = _live_attestations(
            queued,
            now_ns=time.time_ns(),
            ttl_ns=SESSION_ATTESTATION_TTL_NS,
        )
        matching_actor = [
            record
            for record in records
            if hmac.compare_digest(
                str(record["transport_user_id"]).encode("utf-8"),
                transport_user_id.encode("utf-8"),
            )
        ]
        matching_record = next(
            (
                record
                for record in matching_actor
                if hmac.compare_digest(str(record["content_sha256"]), content_sha256)
            ),
            None,
        )
        if matching_record is not None:
            consumed_id = str(matching_record["record_id"])
            state["attestations"] = [
                record for record in records if str(record["record_id"]) != consumed_id
            ]
            _write_state_unlocked(
                path,
                dir_fd=dir_fd,
                state=state,
                max_attestations=MAX_SESSION_ATTESTATIONS,
                require_existing=True,
            )
            return SessionAttestationConsumeResult(
                SessionAttestationResultKind.MATCHED,
                content_digest_matches=True,
            )

        if len(records) != len(queued):
            state["attestations"] = records
            _write_state_unlocked(
                path,
                dir_fd=dir_fd,
                state=state,
                max_attestations=MAX_SESSION_ATTESTATIONS,
                require_existing=True,
            )
        if not records:
            return SessionAttestationConsumeResult(SessionAttestationResultKind.MISSING)
        if not matching_actor:
            return SessionAttestationConsumeResult(SessionAttestationResultKind.ACTOR_MISMATCH)
        return SessionAttestationConsumeResult(SessionAttestationResultKind.CONTENT_MISMATCH)


__all__ = [
    "MAX_SESSION_ATTESTATIONS",
    "MAX_SESSION_ENV_BYTES",
    "SESSION_ATTESTATION_FUTURE_SKEW_NS",
    "SESSION_ATTESTATION_TTL_NS",
    "SESSION_ENV_ALLOWED_KEYS",
    "SESSION_ENV_IDENTITY_KEYS",
    "SESSION_ENV_SCHEMA_VERSION",
    "SESSION_ENV_TRANSPORT_KEYS",
    "SessionEnvSecurityError",
    "consume_session_attestation",
    "normalized_session_env_dir",
    "read_session_identity",
    "read_session_identity_from_path",
    "session_env_digest_from_path",
    "session_env_filename",
    "session_env_lock_filename",
    "session_env_lock_name_from_path",
    "session_env_path",
    "session_env_path_from_environment",
    "session_key_digest",
    "write_session_env",
]
