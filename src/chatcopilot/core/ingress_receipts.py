"""Private, non-authoritative receipts for correlating platform ingress evidence."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows does not run the WSL QQ proxy
    fcntl = None  # type: ignore[assignment]


INGRESS_RECEIPT_SCHEMA_VERSION = 1
INGRESS_RECEIPT_TTL_NS = 120 * 1_000_000_000
MAX_INGRESS_RECEIPTS = 64
_MAX_STATE_BYTES = 128 * 1024
_STATE_NAME = "receipts.json"
_LOCK_NAME = ".receipts.lock"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class IngressReceiptError(RuntimeError):
    """The optional ingress receipt store is unavailable or unsafe."""


@dataclass(frozen=True)
class IngressReceiptMatch:
    status: str
    receipt: Mapping[str, object] | None = None
    reason: str = ""


def receipt_root_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    values = os.environ if env is None else env
    explicit = str(values.get("CHATCOPILOT_INGRESS_RECEIPT_DIR") or "").strip()
    if explicit:
        root = Path(explicit).expanduser()
    else:
        cc_home = str(values.get("CHATCOPILOT_CC_HOME") or "").strip()
        if not cc_home:
            return None
        root = Path(cc_home).expanduser() / "ingress-receipts"
    if not root.is_absolute():
        raise IngressReceiptError("ingress receipt directory must be absolute")
    return root


def append_ingress_receipt(
    root: Path,
    *,
    platform: str,
    chat_kind: str,
    chat_id: str,
    actor_id: str,
    content: str,
    message_id: object = None,
    message_kind: str = "text",
    segment_count: int = 0,
    decision: Mapping[str, object],
    now_ns: int | None = None,
) -> Mapping[str, object]:
    """Append a digest-only receipt; callers must not use it for authorization."""

    normalized_platform = _bounded_token(platform, allowed={"qq"})
    normalized_chat_kind = _bounded_token(chat_kind, allowed={"p2p", "group"})
    if not chat_id or not actor_id:
        raise IngressReceiptError("ingress receipt identity is incomplete")
    if message_kind != "text":
        raise IngressReceiptError("only lossless pure-text ingress can be correlated")
    created_at_ns = time.time_ns() if now_ns is None else now_ns
    if not isinstance(created_at_ns, int) or isinstance(created_at_ns, bool) or created_at_ns <= 0:
        raise IngressReceiptError("ingress receipt timestamp is invalid")
    receipt = {
        "receipt_id": secrets.token_hex(16),
        "created_at_ns": created_at_ns,
        "platform": normalized_platform,
        "chat_kind": normalized_chat_kind,
        "conversation_sha256": _digest(chat_id),
        "actor_sha256": _digest(actor_id),
        "content_sha256": _digest((content or "").strip()),
        "message_id_sha256": (
            _digest(str(message_id))
            if message_id is not None and str(message_id) != ""
            else ""
        ),
        "message_kind": "text",
        "segment_count": _safe_count(segment_count, maximum=4096),
        "decision": _validate_decision(decision),
    }
    with _locked_state(root, create=True) as directory_fd:
        state = _read_state(directory_fd)
        live = _live_receipts(list(state["receipts"]), now_ns=created_at_ns)
        live.append(receipt)
        state["receipts"] = live[-MAX_INGRESS_RECEIPTS:]
        _write_state(directory_fd, state)
    return receipt


def consume_ingress_receipt(
    root: Path,
    *,
    platform: str,
    chat_kind: str,
    chat_id: str,
    actor_id: str,
    content: str,
    now_ns: int | None = None,
) -> IngressReceiptMatch:
    """Consume one exact receipt, leaving duplicate candidates unmatched."""

    normalized_platform = _bounded_token(platform, allowed={"qq"})
    normalized_chat_kind = _bounded_token(chat_kind, allowed={"p2p", "group"})
    if not chat_id or not actor_id:
        return IngressReceiptMatch(status="missing", reason="identity_incomplete")
    observed_at_ns = time.time_ns() if now_ns is None else now_ns
    expected = (
        normalized_platform,
        normalized_chat_kind,
        _digest(chat_id),
        _digest(actor_id),
        _digest((content or "").strip()),
    )
    with _locked_state(root, create=False) as directory_fd:
        state = _read_state(directory_fd)
        queued = list(state["receipts"])
        live = _live_receipts(queued, now_ns=observed_at_ns)
        candidates = [
            receipt
            for receipt in live
            if (
                receipt["platform"],
                receipt["chat_kind"],
                receipt["conversation_sha256"],
                receipt["actor_sha256"],
                receipt["content_sha256"],
            )
            == expected
        ]
        if len(candidates) == 1:
            matched = candidates[0]
            matched_id = str(matched["receipt_id"])
            state["receipts"] = [
                receipt
                for receipt in live
                if str(receipt["receipt_id"]) != matched_id
            ]
            _write_state(directory_fd, state)
            return IngressReceiptMatch(status="matched", receipt=matched)
        if live != queued:
            state["receipts"] = live
            _write_state(directory_fd, state)
        if len(candidates) > 1:
            return IngressReceiptMatch(status="ambiguous", reason="duplicate_candidates")
        return IngressReceiptMatch(status="missing", reason="no_exact_candidate")


def _validate_decision(value: Mapping[str, object]) -> Mapping[str, object]:
    code = str(value.get("code") or "").strip()
    outcome = str(value.get("outcome") or "").strip()
    if not code or len(code) > 120 or outcome not in {"forward", "drop"}:
        raise IngressReceiptError("ingress decision is invalid")
    return {
        "code": code,
        "outcome": outcome,
        "user_allowed": bool(value.get("user_allowed")),
        "group_allowed": bool(value.get("group_allowed")),
        "mention_required": bool(value.get("mention_required")),
        "mention_satisfied": bool(value.get("mention_satisfied")),
        "authoritative": False,
    }


def _validate_receipt(value: object) -> Mapping[str, object]:
    expected_keys = {
        "receipt_id",
        "created_at_ns",
        "platform",
        "chat_kind",
        "conversation_sha256",
        "actor_sha256",
        "content_sha256",
        "message_id_sha256",
        "message_kind",
        "segment_count",
        "decision",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise IngressReceiptError("ingress receipt schema is invalid")
    receipt_id = value.get("receipt_id")
    created_at_ns = value.get("created_at_ns")
    digests = (
        value.get("conversation_sha256"),
        value.get("actor_sha256"),
        value.get("content_sha256"),
    )
    message_digest = value.get("message_id_sha256")
    if (
        not isinstance(receipt_id, str)
        or not _RECEIPT_ID_RE.fullmatch(receipt_id)
        or not isinstance(created_at_ns, int)
        or isinstance(created_at_ns, bool)
        or created_at_ns <= 0
        or any(not isinstance(item, str) or not _DIGEST_RE.fullmatch(item) for item in digests)
        or not isinstance(message_digest, str)
        or (bool(message_digest) and not _DIGEST_RE.fullmatch(message_digest))
        or value.get("message_kind") != "text"
    ):
        raise IngressReceiptError("ingress receipt fields are invalid")
    return {
        "receipt_id": receipt_id,
        "created_at_ns": created_at_ns,
        "platform": _bounded_token(value.get("platform"), allowed={"qq"}),
        "chat_kind": _bounded_token(value.get("chat_kind"), allowed={"p2p", "group"}),
        "conversation_sha256": digests[0],
        "actor_sha256": digests[1],
        "content_sha256": digests[2],
        "message_id_sha256": message_digest,
        "message_kind": "text",
        "segment_count": _safe_count(value.get("segment_count"), maximum=4096),
        "decision": _validate_decision(
            value.get("decision") if isinstance(value.get("decision"), dict) else {}
        ),
    }


def _live_receipts(
    receipts: list[Mapping[str, object]],
    *,
    now_ns: int,
) -> list[Mapping[str, object]]:
    cutoff = now_ns - INGRESS_RECEIPT_TTL_NS
    future_limit = now_ns + 5 * 1_000_000_000
    live: list[Mapping[str, object]] = []
    for receipt in receipts:
        created_at_ns = int(receipt["created_at_ns"])
        if created_at_ns > future_limit:
            raise IngressReceiptError("ingress receipt timestamp is invalid")
        if created_at_ns >= cutoff:
            live.append(receipt)
    return live


@contextmanager
def _locked_state(root: Path, *, create: bool) -> Iterator[int]:
    if fcntl is None:
        raise IngressReceiptError("ingress receipt locking is unavailable")
    try:
        directory_fd = _open_private_root(root, create=create)
    except IngressReceiptError:
        raise
    except OSError as exc:
        raise IngressReceiptError("ingress receipt store is unavailable or unsafe") from exc
    lock_fd: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(_LOCK_NAME, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(lock_fd, 0o600)
        _validate_private_file(os.fstat(lock_fd), label="ingress receipt lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield directory_fd
    except FileNotFoundError as exc:
        raise IngressReceiptError("ingress receipt store is unavailable") from exc
    except OSError as exc:
        raise IngressReceiptError("ingress receipt store is unsafe") from exc
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


def _open_private_root(root: Path, *, create: bool) -> int:
    absolute = root.absolute()
    parent = absolute.parent
    parent_fd = _open_directory_no_symlinks(parent)
    try:
        if create:
            try:
                os.mkdir(absolute.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(absolute.name, flags, dir_fd=parent_fd)
    except OSError:
        os.close(parent_fd)
        raise
    os.close(parent_fd)
    try:
        current = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o700
            or (os.name == "posix" and current.st_uid != os.geteuid())
        ):
            raise IngressReceiptError("ingress receipt directory is unsafe")
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_directory_no_symlinks(path: Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor or os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_state(directory_fd: int) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(_STATE_NAME, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return {"schema_version": INGRESS_RECEIPT_SCHEMA_VERSION, "receipts": []}
    try:
        current = os.fstat(file_fd)
        _validate_private_file(current, label="ingress receipt state")
        if current.st_size > _MAX_STATE_BYTES:
            raise IngressReceiptError("ingress receipt state is too large")
        raw = bytearray()
        while len(raw) <= _MAX_STATE_BYTES:
            chunk = os.read(file_fd, min(16384, _MAX_STATE_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(file_fd)
    if len(raw) > _MAX_STATE_BYTES:
        raise IngressReceiptError("ingress receipt state is too large")
    try:
        value = json.loads(bytes(raw).decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressReceiptError("ingress receipt state is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "receipts"}
        or value.get("schema_version") != INGRESS_RECEIPT_SCHEMA_VERSION
        or not isinstance(value.get("receipts"), list)
        or len(value["receipts"]) > MAX_INGRESS_RECEIPTS
    ):
        raise IngressReceiptError("ingress receipt state schema is invalid")
    receipts = [_validate_receipt(item) for item in value["receipts"]]
    receipt_ids = [str(item["receipt_id"]) for item in receipts]
    if len(receipt_ids) != len(set(receipt_ids)):
        raise IngressReceiptError("ingress receipt identities are not unique")
    return {"schema_version": INGRESS_RECEIPT_SCHEMA_VERSION, "receipts": receipts}


def _write_state(directory_fd: int, state: Mapping[str, object]) -> None:
    receipts_value = state.get("receipts")
    if not isinstance(receipts_value, list) or len(receipts_value) > MAX_INGRESS_RECEIPTS:
        raise IngressReceiptError("ingress receipt state exceeds its bound")
    payload = {
        "schema_version": INGRESS_RECEIPT_SCHEMA_VERSION,
        "receipts": [_validate_receipt(item) for item in receipts_value],
    }
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_STATE_BYTES:
        raise IngressReceiptError("ingress receipt state is too large")
    temporary_name = f".{_STATE_NAME}.{secrets.token_hex(16)}.tmp"
    file_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(file_fd, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(file_fd, encoded[offset:])
            if written <= 0:
                raise OSError("short ingress receipt state write")
            offset += written
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(temporary_name, _STATE_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise IngressReceiptError("ingress receipt state write failed") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _validate_private_file(value: os.stat_result, *, label: str) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
        or (os.name == "posix" and value.st_uid != os.geteuid())
    ):
        raise IngressReceiptError(f"{label} is unsafe")


def _bounded_token(value: object, *, allowed: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise IngressReceiptError("ingress receipt token is invalid")
    return normalized


def _safe_count(value: object, *, maximum: int) -> int:
    if isinstance(value, bool):
        return 0
    try:
        normalized = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return normalized if 0 <= normalized <= maximum else 0


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "INGRESS_RECEIPT_SCHEMA_VERSION",
    "INGRESS_RECEIPT_TTL_NS",
    "IngressReceiptError",
    "IngressReceiptMatch",
    "append_ingress_receipt",
    "consume_ingress_receipt",
    "receipt_root_from_env",
]
