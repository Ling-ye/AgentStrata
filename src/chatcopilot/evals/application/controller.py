"""Persistent application control plane for unified evaluations."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

from chatcopilot.evals.application.bots import (
    EvaluationBotRef,
    EvaluationBotResolver,
    bot_env,
    bot_spec_path,
    evaluation_subprocess_env,
    temporary_eval_env,
)
from chatcopilot.evals.redaction import collect_env_secrets, sanitize_text

ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {
    "completed",
    "partial",
    "cancelled",
    "interrupted",
    "error",
}
MAINTENANCE_FILENAME = ".maintenance.json"
ValidationResult = Mapping[str, Any]
Validator = Callable[[EvaluationBotRef, Mapping[str, Any]], ValidationResult]
BotResolver = Callable[[str], EvaluationBotRef]
WorkerPidStatus = Literal["matched", "unknown", "exited"]
KEEPALIVE = "\x00"
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}
LOGGER = logging.getLogger(__name__)


def _root_thread_lock(root: Path) -> threading.RLock:
    key = str(root)
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    """Hold a process-safe advisory lock for a short control-plane mutation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _validate_private_file_metadata(
            os.fstat(descriptor),
            path,
            label="Evaluation service lock",
        )
    except Exception:
        os.close(descriptor)
        raise
    handle = os.fdopen(descriptor, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            getattr(msvcrt, "locking")(
                handle.fileno(),
                getattr(msvcrt, "LK_LOCK"),
                1,
            )
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    getattr(msvcrt, "locking")(
                        handle.fileno(),
                        getattr(msvcrt, "LK_UNLCK"),
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class EvaluationBlocked(ValueError):
    """Raised when an evaluation request fails readiness checks."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        super().__init__(str(self.payload.get("message") or "evaluation validation failed"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_private_file_metadata(
    metadata: os.stat_result,
    path: Path,
    *,
    label: str = "evaluation artifact",
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} is not a regular file: {path.name}")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} must be owned by the service user: {path.name}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"{label} must use mode 0600: {path.name}")
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} must have exactly one hard link: {path.name}")


def _validate_private_directory_metadata(
    metadata: os.stat_result,
    path: Path,
    *,
    label: str,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is not a directory: {path.name}")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} must be owned by the service user: {path.name}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError(f"{label} must use mode 0700: {path.name}")


def _reject_symlink_components(path: Path) -> None:
    """Reject every existing symlink in an absolute path before mutation."""

    absolute = path if path.is_absolute() else path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"Evaluation path cannot contain a symlink: {current}")


def _read_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"evaluation artifact cannot be opened safely: {path.name}") from exc
    try:
        _validate_private_file_metadata(os.fstat(descriptor), path)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except json.JSONDecodeError:
        return {}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return value if isinstance(value, dict) else {}


@contextmanager
def _open_private_text(
    path: Path,
    *,
    errors: str | None = None,
) -> Iterator[Any]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"evaluation artifact cannot be opened safely: {path.name}") from exc
    try:
        _validate_private_file_metadata(os.fstat(descriptor), path)
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors=errors,
        ) as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    canonical: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        _validate_private_file_metadata(existing, path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if canonical:
                encoded = json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            else:
                encoded = json.dumps(dict(payload), ensure_ascii=False, indent=2)
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_request(
    bot: EvaluationBotRef,
    request: Mapping[str, Any],
    repository_root: Path,
    *,
    env_values: Mapping[str, str] | None = None,
) -> ValidationResult:
    from chatcopilot.evals.evaluations import validate_evaluation

    values = env_values if env_values is not None else bot_env(bot, repository_root)
    with temporary_eval_env(values):
        return validate_evaluation(_core_request(bot, request, repository_root))


def _bot_spec_sha256(bot: EvaluationBotRef, repository_root: Path) -> str:
    path = bot_spec_path(bot, repository_root)
    return sha256(path.read_bytes()).hexdigest()


def _core_request(
    bot: EvaluationBotRef,
    request: Mapping[str, Any],
    repository_root: Path,
    *,
    evaluation_id: str | None = None,
) -> dict[str, Any]:
    kind = str(request.get("kind") or "")
    common: dict[str, Any] = {
        "kind": kind,
        "bot": str(bot_spec_path(bot, repository_root)),
    }
    if evaluation_id:
        common["evaluation_id"] = evaluation_id
    if kind == "comparison":
        value = {
            **common,
            "profile": str(request.get("profile_id") or request.get("profile") or ""),
            "preset": str(request.get("preset") or "quick"),
        }
        if value["preset"] == "custom":
            value.update(
                {
                    "targets": list(request.get("target_ids") or request.get("targets") or ()),
                    "case_refs": list(request.get("case_refs") or ()),
                    "repetitions": request.get("repetitions"),
                    "max_wall_seconds": request.get("max_wall_seconds"),
                    "seed": request.get("seed"),
                }
            )
        return value
    if kind == "suite":
        preset = str(request.get("preset") or "")
        case_ids = list(request.get("case_ids") or ())
        # Named presets own their Case selection. Stored resolved Cases remain
        # descriptive metadata and must not become explicit Core input.
        if preset and preset != "custom":
            case_ids = []
        return {
            **common,
            "suite": str(request.get("suite_id") or request.get("suite") or ""),
            "case_ids": case_ids,
            "preset": preset,
            "repetitions": request.get("repetitions", 1),
            "max_wall_seconds": request.get("max_wall_seconds", 0),
            "seed": request.get("seed", 0),
            "options": dict(request.get("options") or {}),
            # Preserve raw scalar types until the Core request parser applies
            # strict booleans. In particular, bool("false") is True and must
            # never satisfy a one-shot external-write confirmation.
            "confirm_external_write": request.get("confirm_external_write", False),
            "dry_run": request.get("dry_run", False),
            "llm_judge": request.get("llm_judge", False),
        }
    raise ValueError(f"unsupported evaluation kind: {kind}")


def _stored_request(
    bot: EvaluationBotRef,
    request: Mapping[str, Any],
    effective: Mapping[str, Any],
    targets: Sequence[Any],
) -> dict[str, Any]:
    kind = str(request.get("kind") or "")
    value: dict[str, Any] = {
        "kind": kind,
        "bot_id": bot.instance_id,
    }
    if kind == "comparison":
        value.update(
            {
                "profile_id": str(effective.get("profile") or request.get("profile_id") or ""),
                "preset": str(effective.get("preset") or request.get("preset") or "quick"),
                "target_ids": list(effective.get("targets") or request.get("target_ids") or ()),
                "case_refs": list(effective.get("case_refs") or request.get("case_refs") or ()),
                "repetitions": effective.get(
                    "repetitions",
                    request.get("repetitions"),
                ),
                "max_wall_seconds": effective.get(
                    "max_wall_seconds",
                    request.get("max_wall_seconds"),
                ),
                "seed": effective.get("seed", request.get("seed")),
            }
        )
    elif kind == "suite":
        value.update(
            {
                "suite_id": str(effective.get("suite") or request.get("suite_id") or ""),
                "case_ids": list(effective.get("case_ids") or request.get("case_ids") or ()),
                "preset": str(effective.get("preset") or request.get("preset") or ""),
                "repetitions": effective.get(
                    "repetitions",
                    request.get("repetitions", 1),
                ),
                "max_wall_seconds": effective.get(
                    "max_wall_seconds",
                    request.get("max_wall_seconds", 0),
                ),
                "seed": effective.get("seed", request.get("seed", 0)),
                "options": dict(effective.get("options") or request.get("options") or {}),
                "confirm_external_write": bool(
                    effective.get(
                        "confirm_external_write",
                        request.get("confirm_external_write", False),
                    )
                ),
                "dry_run": bool(
                    effective.get(
                        "dry_run",
                        request.get("dry_run", False),
                    )
                ),
                "llm_judge": bool(
                    effective.get(
                        "llm_judge",
                        request.get("llm_judge", False),
                    )
                ),
            }
        )
    else:
        raise ValueError(f"unsupported evaluation kind: {kind}")
    value["targets"] = [dict(item) if isinstance(item, Mapping) else item for item in targets]
    return value


def _start_request_fingerprint(request: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(request),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluation request is not canonical JSON") from exc
    return sha256(encoded).hexdigest()


class EvaluationApplication:
    def __init__(
        self,
        root: Path | None = None,
        *,
        repository_root: Path | None = None,
        validator: Validator | None = None,
        bot_resolver: BotResolver | None = None,
    ) -> None:
        repository_value = (repository_root or Path.cwd()).expanduser()
        if not repository_value.is_absolute():
            repository_value = repository_value.absolute()
        _reject_symlink_components(repository_value)
        self.repository_root = repository_value
        root_value = root or self.repository_root / "reports" / "evals" / "evaluations"
        root_path = root_value.expanduser()
        if not root_path.is_absolute():
            root_path = self.repository_root / root_path
        self.root = root_path.absolute()
        _reject_symlink_components(self.root)
        if root is None:
            try:
                self.root.relative_to(self.repository_root)
            except ValueError as exc:
                raise ValueError("Default Evaluation root must stay inside the repository") from exc
        self._ensure_private_root()
        self._resolve_bot = bot_resolver or EvaluationBotResolver(self.repository_root)
        self._validator = validator
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._process_bot_ids: dict[str, str] = {}
        self._spawn_env_snapshots: dict[str, dict[str, str]] = {}
        self._cancelled: set[str] = set()
        self._recover_interrupted()

    def _ensure_private_root(self) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        _reject_symlink_components(self.root)
        if os.name != "nt":
            self.root.chmod(0o700)
        self._verify_private_root()

    def _verify_private_root(self) -> None:
        _reject_symlink_components(self.root)
        _validate_private_directory_metadata(
            self.root.lstat(),
            self.root,
            label="Evaluation root",
        )

    def start(
        self,
        *,
        bot_id: str,
        request: Mapping[str, Any],
        evaluation_id: str | None = None,
    ) -> dict[str, Any]:
        bot = self._resolve_bot(bot_id)
        effective_env = evaluation_subprocess_env(bot_env(bot, self.repository_root))
        bot_spec_digest = _bot_spec_sha256(bot, self.repository_root)
        clean_request = dict(request)
        clean_request["bot_id"] = bot.instance_id
        request_fingerprint = _start_request_fingerprint(clean_request)
        requested_id = str(evaluation_id or "").strip()
        if requested_id:
            self._evaluation_dir(requested_id)
        with self._creation_guard(), self._lock:
            if requested_id and self._evaluation_dir(requested_id).exists():
                return self._idempotent_start_result(
                    requested_id,
                    bot_id=bot.instance_id,
                    request_fingerprint=request_fingerprint,
                )
            self._require_creation_allowed_locked()
            active = self._active_for_bot_locked(bot.instance_id)
            if active is not None:
                raise RuntimeError(
                    f"Bot {bot.instance_id} already has an active evaluation: "
                    f"{active['evaluation_id']}"
                )

        with temporary_eval_env(effective_env):
            validation = dict(
                self._validator(bot, clean_request)
                if self._validator is not None
                else _validate_request(
                    bot,
                    clean_request,
                    self.repository_root,
                    env_values=effective_env,
                )
            )
        if _bot_spec_sha256(bot, self.repository_root) != bot_spec_digest:
            raise EvaluationBlocked(
                {
                    "code": "configuration_changed",
                    "message": "BotSpec changed during Evaluation preflight; retry the manual run",
                    "checks": [
                        {
                            "id": "bot_spec_snapshot",
                            "label": "BotSpec immutable snapshot",
                            "ok": False,
                            "detail": "configuration changed during preflight",
                            "remediation": "review the BotSpec change and start Evaluation again",
                        }
                    ],
                }
            )
        if not validation.get("ready"):
            payload = {
                "code": str(validation.get("code") or "evaluation_blocked"),
                "message": str(validation.get("message") or "评测条件未满足，未创建评测记录"),
                "checks": list(validation.get("checks") or ()),
            }
            raise EvaluationBlocked(payload)

        effective = validation.get("effective_request")
        if not isinstance(effective, Mapping):
            effective = {}
        targets = validation.get("targets")
        target_values: Sequence[Any] = ()
        if isinstance(targets, Sequence) and not isinstance(
            targets,
            (str, bytes),
        ):
            target_values = targets
        clean_request = _stored_request(
            bot,
            clean_request,
            effective,
            target_values,
        )

        with self._creation_guard(), self._lock:
            if requested_id and self._evaluation_dir(requested_id).exists():
                return self._idempotent_start_result(
                    requested_id,
                    bot_id=bot.instance_id,
                    request_fingerprint=request_fingerprint,
                )
            self._require_creation_allowed_locked()
            active = self._active_for_bot_locked(bot.instance_id)
            if active is not None:
                raise RuntimeError(
                    f"Bot {bot.instance_id} already has an active evaluation: "
                    f"{active['evaluation_id']}"
                )
            evaluation_id = requested_id or (
                "eval-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            )
            directory = self._evaluation_dir(evaluation_id)
            created_at = _utc_now()
            stored_request = {
                **clean_request,
                "evaluation_id": evaluation_id,
                "bot_id": bot.instance_id,
                "start_request_fingerprint": request_fingerprint,
                "bot_spec": str(
                    bot_spec_path(bot, self.repository_root).relative_to(self.repository_root)
                ),
                "bot_spec_sha256": bot_spec_digest,
                "created_at": created_at,
            }
            core_request = _core_request(
                bot,
                stored_request,
                self.repository_root,
                evaluation_id=evaluation_id,
            )
            core_request["bot"] = stored_request["bot_spec"]
            stored_request["core_request"] = core_request
            state = {
                "evaluation_id": evaluation_id,
                "kind": str(clean_request.get("kind") or ""),
                "status": "queued",
                "pid": None,
                "started_at": None,
                "finished_at": None,
                "duration_seconds": None,
                "completed_trials": 0,
                "planned_trials": self._planned_trial_count(
                    stored_request,
                    {},
                ),
                "error": None,
            }
            try:
                self._create_claim(bot.instance_id, evaluation_id)
                directory.mkdir(parents=True, mode=0o700)
                _write_json(directory / "request.json", stored_request)
                _write_json(directory / "state.json", state)
                self._spawn_env_snapshots[evaluation_id] = effective_env.copy()
                try:
                    self._spawn(evaluation_id, bot)
                finally:
                    self._spawn_env_snapshots.pop(evaluation_id, None)
            except Exception as exc:
                safe_error = self._sanitize_startup_error(
                    bot,
                    directory,
                    exc,
                    env_values=effective_env,
                )
                state.update(
                    {
                        "status": "error",
                        "finished_at": _utc_now(),
                        "error": safe_error,
                    }
                )
                if directory.is_dir():
                    _write_json(directory / "state.json", state)
                process = self._processes.pop(evaluation_id, None)
                self._process_bot_ids.pop(evaluation_id, None)
                if process is not None:
                    self._terminate_process(process)
                self._release_claim(bot.instance_id, evaluation_id)
                raise RuntimeError(safe_error) from exc
        return self.get(evaluation_id)

    def list(
        self,
        *,
        kind: str | None = None,
        bot_id: str | None = None,
        target: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._verify_private_root()
        values: list[dict[str, Any]] = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                item = self.get(path.name, include_result=False)
            except (KeyError, ValueError):
                continue
            if kind and item.get("kind") != kind:
                continue
            if bot_id and item.get("bot_id") != bot_id:
                continue
            if status and item.get("status") != status:
                continue
            if target and target not in {
                str(value.get("target_id") or "")
                for value in item.get("targets", [])
                if isinstance(value, Mapping)
            }:
                continue
            values.append(item)
        values.sort(
            key=lambda value: str(value.get("created_at") or ""),
            reverse=True,
        )
        return values

    def active_count(self) -> int:
        """Count active lifecycle records without reading large Core results."""

        return int(self.update_readiness()["active_count"])

    def maintenance_status(self) -> dict[str, Any] | None:
        """Return the persisted maintenance lease, if one is active."""

        with self._lock:
            return self._read_maintenance_locked()

    def enter_maintenance(self, lease_id: str) -> dict[str, Any]:
        """Atomically prove idle and prevent new Evaluation creation."""

        lease = self._validated_lease_id(lease_id)
        with self._creation_guard(), self._lock:
            existing = self._read_maintenance_locked()
            if existing is not None:
                if existing.get("lease_id") == lease:
                    return dict(existing)
                raise RuntimeError("Evaluation maintenance is already active")
            readiness = self.update_readiness()
            if (
                readiness.get("idle_proven") is not True
                or int(readiness.get("active_count") or 0) != 0
            ):
                raise RuntimeError("Evaluation is active or idle state cannot be proven")
            payload = {
                "maintenance": True,
                "schema_version": 1,
                "lease_id": lease,
                "created_at": _utc_now(),
                "owner_pid": os.getpid(),
            }
            _write_json(self.root / MAINTENANCE_FILENAME, payload)
            return dict(payload)

    def leave_maintenance(self, lease_id: str) -> dict[str, Any]:
        """Release the matching persisted maintenance lease."""

        lease = self._validated_lease_id(lease_id)
        with self._creation_guard(), self._lock:
            existing = self._read_maintenance_locked()
            if existing is None:
                return {"maintenance": False, "lease_id": lease}
            if existing.get("lease_id") != lease:
                raise RuntimeError("Evaluation maintenance lease does not match")
            path = self.root / MAINTENANCE_FILENAME
            self._verify_artifact_file(path, required=True)
            path.unlink()
            return {"maintenance": False, "lease_id": lease}

    def update_readiness(self) -> dict[str, Any]:
        """Return a fail-closed snapshot proving whether code may be updated.

        This method deliberately takes only the re-entrant in-process lock. Callers
        that need a transactional idle-to-maintenance transition must hold the
        process-wide creation guard around this snapshot and their state change.
        """

        with self._lock:
            self._verify_private_root()
            active_count = 0
            idle_proven = True
            for path in self.root.iterdir():
                if not path.name.startswith("eval-"):
                    continue
                try:
                    directory = self._evaluation_dir(path.name)
                    state_path = directory / "state.json"
                    self._verify_artifact_file(state_path, required=True)
                    state = _read_json(state_path)
                except (KeyError, OSError, PermissionError, RuntimeError, ValueError):
                    idle_proven = False
                    continue
                if not state or state.get("evaluation_id") != path.name:
                    idle_proven = False
                    continue
                status = state.get("status")
                if status in ACTIVE_STATUSES:
                    active_count += 1
                    idle_proven = False
                    continue
                if status not in TERMINAL_STATUSES:
                    idle_proven = False
                    continue

                request: dict[str, Any] = {}
                try:
                    request_path = directory / "request.json"
                    self._verify_artifact_file(request_path, required=True)
                    request = _read_json(request_path)
                except (KeyError, OSError, PermissionError, RuntimeError, ValueError):
                    idle_proven = False
                if not request or request.get("evaluation_id") != path.name:
                    idle_proven = False
                bot_id = str(request.get("bot_id") or "")
                process = self._processes.get(path.name)
                has_local_process = process is not None and process.poll() is None
                if isinstance(state.get("pid"), int) or has_local_process:
                    try:
                        if self._evaluation_is_live(
                            path.name,
                            bot_id=bot_id,
                            state=state,
                        ):
                            idle_proven = False
                    except (OSError, PermissionError, RuntimeError, ValueError):
                        idle_proven = False

            # A claim can exist before the lifecycle directory is published, and a
            # surviving worker can retain one after a contradictory terminal state.
            # Either condition means absence of active work has not been proven.
            if any(process.poll() is None for process in self._processes.values()) or any(
                self.root.glob(".active-*.json")
            ):
                idle_proven = False
            return {
                "active_count": active_count,
                "idle_proven": idle_proven,
            }

    def get(
        self,
        evaluation_id: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any]:
        # The worker finalizer writes terminal state and releases the Bot claim
        # under this same lock.  Read that boundary atomically so callers never
        # observe "completed" while the activity claim still blocks the next
        # manually requested Evaluation.
        with self._lock:
            directory = self._verified_evaluation_dir(evaluation_id)
            state = _read_json(directory / "state.json")
            request = _read_json(directory / "request.json")
            result = self._verified_result(
                evaluation_id,
                directory=directory,
                required=False,
            )
        trials = result.get("trials")
        trial_values = trials if isinstance(trials, list) else []
        targets = result.get("targets")
        if not isinstance(targets, list):
            targets = request.get("targets")
        target_values = targets if isinstance(targets, list) else []
        total = self._planned_trial_count(request, result)
        completed = len(trial_values)
        progress = {
            "completed": completed,
            "total": total,
            "percent": min(100, round(completed / total * 100)) if total else 0,
        }
        response = {
            **state,
            "kind": str(request.get("kind") or state.get("kind") or ""),
            "bot_id": str(request.get("bot_id") or ""),
            "created_at": str(request.get("created_at") or ""),
            "targets": target_values,
            "progress": progress,
            "duration_seconds": result.get(
                "duration_seconds",
                state.get("duration_seconds"),
            ),
            "summary": result.get("summary") if isinstance(result.get("summary"), Mapping) else {},
            "selection": self._selection_summary(request),
        }
        if include_result:
            response["request"] = request
            response["result"] = result
        return response

    def case_detail(
        self,
        evaluation_id: str,
        case_ref: str,
    ) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=True,
        )
        comparisons = result.get("comparisons")
        comparison = (
            next(
                (
                    value
                    for value in comparisons
                    if isinstance(value, Mapping)
                    and str(value.get("case_ref") or value.get("case_id") or "") == case_ref
                ),
                None,
            )
            if isinstance(comparisons, list)
            else None
        )
        trials = [
            value
            for value in result.get("trials", [])
            if isinstance(value, Mapping)
            and case_ref
            in {
                str(value.get("case_ref") or ""),
                str(value.get("case_id") or ""),
            }
        ]
        if not trials and comparison is None:
            raise KeyError(case_ref)
        return {
            "case_ref": case_ref,
            "comparison": comparison,
            "trials": trials,
        }

    def active_for_bot(self, bot_id: str) -> dict[str, Any] | None:
        with self._creation_guard(), self._lock:
            return self._active_for_bot_locked(bot_id)

    def clone(
        self,
        evaluation_id: str,
        *,
        new_evaluation_id: str | None = None,
    ) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        stored = _read_json(directory / "request.json")
        return self.start(
            bot_id=str(stored.get("bot_id") or ""),
            request=self._clone_request(stored),
            evaluation_id=new_evaluation_id,
        )

    def _idempotent_start_result(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        stored = _read_json(directory / "request.json")
        if (
            stored.get("bot_id") != bot_id
            or stored.get("start_request_fingerprint") != request_fingerprint
        ):
            raise RuntimeError("evaluation_id already belongs to a different start request")
        return self.get(evaluation_id)

    def cancel(self, evaluation_id: str) -> dict[str, Any]:
        bot_id = ""
        worker_pid: int | None = None
        with self._creation_guard(), self._lock:
            state = self._state(evaluation_id)
            if state.get("status") not in ACTIVE_STATUSES:
                raise RuntimeError("only queued or running evaluations can be cancelled")
            request = _read_json(self._evaluation_dir(evaluation_id) / "request.json")
            bot_id = str(request.get("bot_id") or "")
            worker_pid, worker_identity_unknown = self._managed_worker_observation(
                evaluation_id,
                bot_id=bot_id,
                state=state,
            )
            if worker_identity_unknown:
                raise RuntimeError(
                    "evaluation worker still exists but its identity "
                    "cannot be verified; cancellation refused"
                )
            self._write_cancel_marker(evaluation_id)
            self._cancelled.add(evaluation_id)
        if worker_pid is not None:
            self._request_pid_stop(worker_pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._creation_guard(), self._lock:
                current_state = self._state(evaluation_id)
                if not self._evaluation_is_live(
                    evaluation_id,
                    bot_id=bot_id,
                    state=current_state,
                ):
                    if current_state.get("status") in ACTIVE_STATUSES:
                        self._finalize_cancelled(evaluation_id)
                    self._complete_terminal_finalization(
                        evaluation_id,
                        bot_id=bot_id,
                    )
                    return self.get(evaluation_id, include_result=False)
            time.sleep(0.05)
        with self._creation_guard(), self._lock:
            current_state = self._state(evaluation_id)
            worker_pid, worker_identity_unknown = self._managed_worker_observation(
                evaluation_id,
                bot_id=bot_id,
                state=current_state,
            )
            if worker_identity_unknown:
                raise RuntimeError(
                    "evaluation worker identity changed during cancellation; "
                    "forced termination refused"
                )
        if worker_pid is not None:
            self._kill_pid(worker_pid)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not self._pid_exists(worker_pid):
                    with self._creation_guard(), self._lock:
                        current_state = self._state(evaluation_id)
                        if current_state.get("status") in ACTIVE_STATUSES:
                            self._finalize_cancelled(evaluation_id)
                        self._complete_terminal_finalization(
                            evaluation_id,
                            bot_id=bot_id,
                        )
                        return self.get(evaluation_id, include_result=False)
                time.sleep(0.05)
        raise RuntimeError("evaluation process did not stop after cancellation")

    def delete(self, evaluation_id: str) -> None:
        with self._creation_guard(), self._lock:
            state = self._state(evaluation_id)
            request = _read_json(self._verified_evaluation_dir(evaluation_id) / "request.json")
            bot_id = str(request.get("bot_id") or "")
            if self._evaluation_is_live(
                evaluation_id,
                bot_id=bot_id,
                state=state,
            ):
                raise RuntimeError("running evaluations cannot be deleted")
            if state.get("status") in ACTIVE_STATUSES:
                self._reconcile_stopped_evaluation(
                    evaluation_id,
                    "evaluation process is no longer running",
                )
            target = self._verified_evaluation_dir(evaluation_id)
            target.relative_to(self.root)
            if target == self.root or not target.is_dir():
                raise ValueError("invalid evaluation directory")
            shutil.rmtree(target)
            if bot_id:
                self._release_claim(bot_id, evaluation_id)

    def coverage(
        self,
        *,
        bot_id: str | None = None,
    ) -> dict[str, Any]:
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for evaluation in self.list(bot_id=bot_id):
            evaluation_id = str(evaluation["evaluation_id"])
            result = self._verified_result(
                evaluation_id,
                required=False,
            )
            for trial in result.get("trials", []):
                if not isinstance(trial, Mapping):
                    continue
                case_ref = str(
                    trial.get("case_ref")
                    or ":".join(
                        part
                        for part in (
                            str(trial.get("suite_id") or ""),
                            str(trial.get("case_id") or ""),
                        )
                        if part
                    )
                )
                fingerprint = str(trial.get("target_fingerprint") or trial.get("fingerprint") or "")
                current_bot = str(evaluation.get("bot_id") or "")
                if not current_bot or not case_ref or not fingerprint:
                    continue
                key = (current_bot, case_ref, fingerprint)
                target_id = str(trial.get("target_id") or "")
                history_item = {
                    "trial_id": str(trial.get("trial_id") or ""),
                    "attempt": trial.get("attempt"),
                    "evaluation_id": evaluation.get("evaluation_id"),
                    "kind": evaluation.get("kind"),
                    "bot_id": current_bot,
                    "suite_id": str(trial.get("suite_id") or ""),
                    "case_id": str(trial.get("case_id") or ""),
                    "case_ref": case_ref,
                    "category": str(trial.get("dimension") or ""),
                    "target_id": target_id,
                    "target_fingerprint": fingerprint,
                    "executor": str(trial.get("executor") or ""),
                    "backend": str(trial.get("backend") or ""),
                    "model": str(trial.get("model") or ""),
                    "reasoning_effort": str(trial.get("reasoning_effort") or ""),
                    "outcome": str(trial.get("outcome") or ""),
                    "score": trial.get("score"),
                    "max_score": trial.get("max_score"),
                    "duration_seconds": trial.get("duration_seconds"),
                    "finished_at": str(
                        trial.get("finished_at") or evaluation.get("finished_at") or ""
                    ),
                    "error": str(trial.get("error") or ""),
                }
                bucket = buckets.setdefault(
                    key,
                    {
                        "bot_id": current_bot,
                        "suite_id": str(trial.get("suite_id") or ""),
                        "case_id": str(trial.get("case_id") or ""),
                        "case_ref": case_ref,
                        "category": str(trial.get("dimension") or ""),
                        "summary": "",
                        "target_id": target_id,
                        "target_fingerprint": fingerprint,
                        "completed_count": 0,
                        "history": [],
                    },
                )
                bucket["completed_count"] += 1
                bucket["history"].append(history_item)

        records: list[dict[str, Any]] = []
        for bucket in buckets.values():
            history = sorted(
                bucket["history"],
                key=lambda value: (
                    str(value.get("finished_at") or ""),
                    str(value.get("evaluation_id") or ""),
                ),
                reverse=True,
            )
            latest = history[0]
            records.append(
                {
                    **{key: value for key, value in bucket.items() if key != "history"},
                    "last_evaluation_id": latest["evaluation_id"],
                    "last_outcome": latest["outcome"],
                    "last_score": latest["score"],
                    "last_max_score": latest["max_score"],
                    "last_duration_seconds": latest["duration_seconds"],
                    "last_completed_at": latest["finished_at"],
                    "history": history,
                }
            )
        records.sort(
            key=lambda value: (
                str(value.get("last_completed_at") or ""),
                str(value.get("case_ref") or ""),
            ),
            reverse=True,
        )
        return {
            "generated_at": _utc_now(),
            "summary": {
                "case_count": len({(item["bot_id"], item["case_ref"]) for item in records}),
                "failed_case_count": len(
                    {
                        (item["bot_id"], item["case_ref"])
                        for item in records
                        if item.get("last_outcome") in {"failed", "error"}
                    }
                ),
                "bot_count": len({item["bot_id"] for item in records}),
                "target_count": len({item["target_fingerprint"] for item in records}),
            },
            "records": records,
        }

    def report_path(self, evaluation_id: str, kind: str) -> Path:
        names = {"json": "result.json", "markdown": "summary.md"}
        if kind not in names:
            raise ValueError("report kind must be json or markdown")
        directory = self._verified_evaluation_dir(evaluation_id)
        self._verified_result(
            evaluation_id,
            directory=directory,
            required=True,
        )
        path = directory / names[kind]
        self._verify_artifact_file(path, required=True)
        if not path.is_file():
            raise KeyError(evaluation_id)
        return path

    def follow(
        self,
        evaluation_id: str,
        *,
        poll_seconds: float = 0.25,
    ) -> Iterator[dict[str, Any] | str]:
        directory = self._verified_evaluation_dir(evaluation_id)
        self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        path = directory / "progress.jsonl"
        position = 0
        idle_since = time.monotonic()
        while True:
            self._verified_evaluation_dir(evaluation_id)
            self._verified_result(
                evaluation_id,
                directory=directory,
                required=False,
            )
            self._verify_artifact_file(path, required=False)
            if path.exists():
                with _open_private_text(path, errors="replace") as handle:
                    handle.seek(position)
                    for line in handle:
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict):
                            yield payload
                    position = handle.tell()
            with self._lock:
                state = _read_json(directory / "state.json")
            if state.get("status") not in ACTIVE_STATUSES:
                yield {
                    "event": "evaluation_status",
                    "evaluation_id": evaluation_id,
                    "status": state.get("status", "error"),
                }
                return
            if time.monotonic() - idle_since >= 15:
                idle_since = time.monotonic()
                yield KEEPALIVE
            time.sleep(poll_seconds)

    def _spawn(
        self,
        evaluation_id: str,
        bot: EvaluationBotRef,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        cancel_path = self._cancel_path(evaluation_id)
        startup_reader, startup_writer = os.pipe()
        os.set_inheritable(startup_reader, True)
        command = [
            sys.executable,
            "-m",
            "chatcopilot.evals.managed_worker",
            "--request",
            str(directory / "request.json"),
            "--output",
            str(directory),
            "--cancel-file",
            str(cancel_path),
            "--log-file",
            str(directory / "run.log"),
            "--startup-fd",
            str(startup_reader),
        ]

        snapshot = self._spawn_env_snapshots.get(evaluation_id)
        env = evaluation_subprocess_env(
            snapshot.copy() if snapshot is not None else bot_env(bot, self.repository_root)
        )
        src = str(self.repository_root / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            [
                src,
                *[item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item],
            ]
        )
        flags = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            if os.name == "nt"
            else 0
        )
        popen_options: dict[str, Any]
        if os.name == "nt":
            popen_options = {"close_fds": False}
        else:
            popen_options = {"pass_fds": (startup_reader,)}
        process: subprocess.Popen[Any]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.repository_root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=False,
                start_new_session=os.name != "nt",
                creationflags=flags,
                **popen_options,
            )
            os.close(startup_reader)
            startup_reader = -1
            self._processes[evaluation_id] = process
            self._process_bot_ids[evaluation_id] = bot.instance_id
            state = self._state(evaluation_id)
            state.update(
                {
                    "status": "running",
                    "started_at": _utc_now(),
                    "pid": process.pid,
                }
            )
            _write_json(directory / "state.json", state)
            self._update_claim(
                bot.instance_id,
                evaluation_id,
                worker_pid=process.pid,
            )
            if os.write(startup_writer, b"\x01") != 1:
                raise RuntimeError("Evaluation worker startup handshake failed")
        finally:
            if startup_reader >= 0:
                os.close(startup_reader)
            os.close(startup_writer)
        threading.Thread(
            target=self._monitor,
            args=(
                evaluation_id,
                process,
                bot.instance_id,
            ),
            name=f"evaluation-{evaluation_id}",
            daemon=True,
        ).start()

    def _monitor(
        self,
        evaluation_id: str,
        process: subprocess.Popen[Any],
        bot_id: str,
    ) -> None:
        exit_code = process.wait()
        self._finalize_worker_exit(
            evaluation_id,
            bot_id=bot_id,
            exit_code=exit_code,
        )

    def _finalize_worker_exit(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
        exit_code: int | None,
    ) -> None:
        with self._creation_guard(), self._lock:
            terminal_state_persisted = False
            try:
                directory = self._verified_evaluation_dir(evaluation_id)
                state_path = directory / "state.json"
                state = _read_json(state_path)
                if not state:
                    raise ValueError("evaluation state is not valid JSON")
                result = self._verified_result(
                    evaluation_id,
                    directory=directory,
                    required=False,
                )
                if result.get("status") in TERMINAL_STATUSES:
                    status = str(result["status"])
                elif evaluation_id in self._cancelled or self._cancel_requested(evaluation_id):
                    status = "cancelled"
                    state["error"] = "evaluation cancelled before Core finalized"
                else:
                    status = "error"
                    state["error"] = "evaluation process exited without a final result"
                state.update(
                    {
                        "status": status,
                        "finished_at": _utc_now(),
                        "pid": None,
                        "duration_seconds": result.get(
                            "duration_seconds",
                            state.get("duration_seconds"),
                        ),
                        "completed_trials": len(result.get("trials", []))
                        if isinstance(result.get("trials"), list)
                        else state.get("completed_trials", 0),
                    }
                )
                if exit_code and not state.get("error"):
                    state["error"] = f"evaluation process exited with code {exit_code}"
                _write_json(directory / "state.json", state)
                terminal_state_persisted = True
            except Exception as exc:
                LOGGER.error(
                    "Evaluation worker finalization failed; activity claim retained "
                    "evaluation_id=%s error_type=%s",
                    evaluation_id,
                    type(exc).__name__,
                )

            self._processes.pop(evaluation_id, None)
            self._process_bot_ids.pop(evaluation_id, None)
            if not terminal_state_persisted:
                return
            try:
                self._complete_terminal_finalization(
                    evaluation_id,
                    bot_id=bot_id,
                )
            except Exception as exc:
                LOGGER.error(
                    "Evaluation terminal cleanup failed; activity claim retained "
                    "evaluation_id=%s error_type=%s",
                    evaluation_id,
                    type(exc).__name__,
                )

    def _recover_interrupted(self) -> None:
        inherited: list[tuple[str, str, int]] = []
        with self._creation_guard(), self._lock:
            self._verify_private_root()
            for path in self.root.iterdir():
                if not path.is_dir():
                    continue
                try:
                    directory = self._evaluation_dir(path.name)
                except ValueError:
                    continue
                state_path = directory / "state.json"
                request_path = directory / "request.json"
                self._verify_artifact_file(state_path, required=False)
                self._verify_artifact_file(request_path, required=False)
                state = _read_json(state_path)
                request = _read_json(request_path)
                bot_id = str(request.get("bot_id") or "")
                worker_pid, identity_unknown = self._managed_worker_observation(
                    path.name,
                    bot_id=bot_id,
                    state=state,
                )
                if worker_pid is not None:
                    if state.get("pid") != worker_pid:
                        state.update(
                            {
                                "status": "running",
                                "pid": worker_pid,
                                "started_at": state.get("started_at") or _utc_now(),
                            }
                        )
                        _write_json(state_path, state)
                    if bot_id:
                        claim = self._read_claim(bot_id)
                        if claim is None:
                            self._create_claim(bot_id, path.name)
                        self._update_claim(
                            bot_id,
                            path.name,
                            worker_pid=worker_pid,
                        )
                    inherited.append((path.name, bot_id, worker_pid))
                    continue
                if identity_unknown:
                    continue
                if state.get("status") in ACTIVE_STATUSES:
                    self._reconcile_stopped_evaluation(
                        path.name,
                        "evaluation service restarted after worker exit",
                    )
                if bot_id:
                    self._release_claim(bot_id, path.name)
            self._remove_stale_orphan_claims()
        for evaluation_id, bot_id, worker_pid in inherited:
            threading.Thread(
                target=self._watch_inherited_worker,
                args=(evaluation_id, bot_id, worker_pid),
                name=f"evaluation-inherited-{evaluation_id}",
                daemon=True,
            ).start()

    def _watch_inherited_worker(
        self,
        evaluation_id: str,
        bot_id: str,
        worker_pid: int,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        while self._worker_pid_status(worker_pid, directory) != "exited":
            time.sleep(0.1)
        self._finalize_worker_exit(
            evaluation_id,
            bot_id=bot_id,
            exit_code=None,
        )

    def _reconcile_stopped_evaluation(
        self,
        evaluation_id: str,
        message: str,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        if not directory.is_dir():
            return
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        result_status = str(result.get("status") or "")
        if result_status not in TERMINAL_STATUSES:
            self._mark_interrupted(evaluation_id, message)
            return
        state = _read_json(directory / "state.json")
        if not state:
            return
        trials = result.get("trials")
        completed_trials = len(trials) if isinstance(trials, list) else 0
        state.update(
            {
                "status": result_status,
                "pid": None,
                "started_at": result.get(
                    "started_at",
                    state.get("started_at"),
                ),
                "finished_at": result.get(
                    "finished_at",
                    state.get("finished_at"),
                ),
                "duration_seconds": result.get(
                    "duration_seconds",
                    state.get("duration_seconds"),
                ),
                "completed_trials": completed_trials,
                "planned_trials": result.get(
                    "planned_trials",
                    state.get("planned_trials"),
                ),
                "error": result.get("error"),
            }
        )
        _write_json(directory / "state.json", state)

    def _mark_interrupted(
        self,
        evaluation_id: str,
        message: str,
    ) -> None:
        directory = self._evaluation_dir(evaluation_id)
        if not directory.is_dir():
            return
        result = self._verified_result(
            evaluation_id,
            directory=directory,
            required=False,
        )
        trials = result.get("trials")
        completed = len(trials) if isinstance(trials, list) else 0
        status = "partial" if completed else "interrupted"
        state = _read_json(directory / "state.json")
        if not state:
            return
        state.update(
            {
                "status": status,
                "finished_at": _utc_now(),
                "pid": None,
                "error": message,
            }
        )
        _write_json(directory / "state.json", state)

    def _finalize_cancelled(self, evaluation_id: str) -> None:
        state = self._state(evaluation_id)
        state.update(
            {
                "status": "cancelled",
                "finished_at": _utc_now(),
                "pid": None,
                "error": "evaluation cancelled by user",
            }
        )
        _write_json(
            self._evaluation_dir(evaluation_id) / "state.json",
            state,
        )

    def _complete_terminal_finalization(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
    ) -> None:
        state = self._state(evaluation_id)
        if state.get("status") not in TERMINAL_STATUSES:
            raise RuntimeError("evaluation terminal state was not persisted")
        self._remove_cancel_marker(evaluation_id)
        self._cancelled.discard(evaluation_id)
        if bot_id:
            self._release_claim(bot_id, evaluation_id)

    def _sanitize_startup_error(
        self,
        bot: EvaluationBotRef,
        directory: Path,
        exc: Exception,
        *,
        env_values: Mapping[str, str] | None = None,
    ) -> str:
        try:
            values = (
                env_values if env_values is not None else bot_env(bot, self.repository_root)
            )
            env = evaluation_subprocess_env(values)
        except Exception:
            env = os.environ.copy()
        return sanitize_text(
            f"{type(exc).__name__}: {exc}",
            secrets=collect_env_secrets(env),
            roots={
                "evaluation": directory,
                "repository": self.repository_root,
            },
        )

    @contextmanager
    def _creation_guard(self) -> Iterator[None]:
        thread_lock = _root_thread_lock(self.root)
        with thread_lock, _locked_file(self.root / ".service.lock"):
            yield

    @staticmethod
    def _validated_lease_id(lease_id: str) -> str:
        lease = str(lease_id or "").strip().lower()
        if len(lease) != 32 or any(character not in "0123456789abcdef" for character in lease):
            raise ValueError("maintenance lease_id must be 32 lowercase hexadecimal characters")
        return lease

    def _read_maintenance_locked(self) -> dict[str, Any] | None:
        self._verify_private_root()
        path = self.root / MAINTENANCE_FILENAME
        try:
            exists = self._verify_artifact_file(path, required=False)
        except (PermissionError, ValueError) as exc:
            raise RuntimeError("Evaluation maintenance marker is unsafe") from exc
        if not exists:
            return None
        try:
            payload = _read_json(path)
        except (PermissionError, ValueError) as exc:
            raise RuntimeError("Evaluation maintenance marker is unsafe") from exc
        lease_id = payload.get("lease_id")
        if (
            payload.get("maintenance") is not True
            or payload.get("schema_version") != 1
            or not isinstance(lease_id, str)
        ):
            raise RuntimeError("Evaluation maintenance marker is unreadable")
        self._validated_lease_id(lease_id)
        return payload

    def _require_creation_allowed_locked(self) -> None:
        if self._read_maintenance_locked() is not None:
            raise RuntimeError("Evaluation maintenance is active; creation is disabled")

    def _active_for_bot_locked(
        self,
        bot_id: str,
    ) -> dict[str, Any] | None:
        claim = self._read_claim(bot_id)
        if claim is not None:
            evaluation_id = str(claim.get("evaluation_id") or "")
            if not evaluation_id:
                raise RuntimeError(f"Bot {bot_id} has an invalid evaluation activity claim")
            directory = self._evaluation_dir(evaluation_id)
            state_path = directory / "state.json"
            self._verify_artifact_file(state_path, required=False)
            state = _read_json(state_path)
            if self._claim_is_live(claim, directory):
                try:
                    return self.get(evaluation_id, include_result=False)
                except KeyError:
                    return {
                        "evaluation_id": evaluation_id,
                        "bot_id": bot_id,
                        "status": "running",
                    }
            if state.get("status") in ACTIVE_STATUSES:
                self._reconcile_stopped_evaluation(
                    evaluation_id,
                    "evaluation process is no longer running",
                )
            self._release_claim(bot_id, evaluation_id)

        for item in self.list(bot_id=bot_id):
            evaluation_id = str(item.get("evaluation_id") or "")
            state = _read_json(self._evaluation_dir(evaluation_id) / "state.json")
            if self._evaluation_is_live(
                evaluation_id,
                bot_id=bot_id,
                state=state,
            ):
                return item
            if state.get("status") in ACTIVE_STATUSES:
                self._reconcile_stopped_evaluation(
                    evaluation_id,
                    "evaluation process is no longer running",
                )
                self._release_claim(bot_id, evaluation_id)
        return None

    def _evaluation_is_live(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        process = self._processes.get(evaluation_id)
        if process is not None and process.poll() is None:
            return True
        worker_pid, identity_unknown = self._managed_worker_observation(
            evaluation_id,
            bot_id=bot_id,
            state=state,
        )
        return worker_pid is not None or identity_unknown

    def _managed_worker_observation(
        self,
        evaluation_id: str,
        *,
        bot_id: str,
        state: Mapping[str, Any],
    ) -> tuple[int | None, bool]:
        candidates: list[int] = []
        state_pid = state.get("pid")
        if isinstance(state_pid, int):
            candidates.append(state_pid)
        if bot_id:
            claim = self._read_claim(bot_id)
            if claim is not None and claim.get("evaluation_id") == evaluation_id:
                worker_pid = claim.get("worker_pid")
                if isinstance(worker_pid, int) and worker_pid not in candidates:
                    candidates.append(worker_pid)
        directory = self._evaluation_dir(evaluation_id)
        identity_unknown = False
        for pid in candidates:
            status = self._worker_pid_status(pid, directory)
            if status == "matched":
                return pid, False
            if status == "unknown":
                identity_unknown = True
        discovered = self._discover_worker_pids(directory)
        if len(discovered) == 1:
            return discovered[0], False
        if len(discovered) > 1:
            identity_unknown = True
        return None, identity_unknown

    @classmethod
    def _discover_worker_pids(cls, directory: Path) -> Sequence[int]:
        """Find same-user managed workers when startup PID persistence was interrupted."""

        if os.name == "nt":
            return []
        proc = Path("/proc")
        try:
            entries = tuple(proc.iterdir())
        except OSError:
            return []
        matches: list[int] = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid <= 0 or pid == os.getpid():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            if cls._pid_matches_evaluation(pid, directory):
                matches.append(pid)
        return sorted(matches)

    def _cancel_path(self, evaluation_id: str) -> Path:
        return self._evaluation_dir(evaluation_id) / ".cancel-requested.json"

    def _write_cancel_marker(self, evaluation_id: str) -> None:
        _write_json(
            self._cancel_path(evaluation_id),
            {
                "evaluation_id": evaluation_id,
                "requested_at": _utc_now(),
            },
            canonical=True,
        )

    def _cancel_requested(self, evaluation_id: str) -> bool:
        path = self._cancel_path(evaluation_id)
        if not self._verify_artifact_file(path, required=False):
            return False
        payload = _read_json(path)
        if payload.get("evaluation_id") != evaluation_id:
            raise ValueError("evaluation cancel marker identity mismatch")
        return True

    def _remove_cancel_marker(self, evaluation_id: str) -> None:
        """Remove only the private marker inode whose identity was verified."""

        path = self._cancel_path(evaluation_id)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError("evaluation cancel marker cannot be opened safely") from exc

        tombstone = path.with_name(f".{path.name}.{uuid.uuid4().hex}.delete")
        moved = False
        try:
            metadata = os.fstat(descriptor)
            _validate_private_file_metadata(
                metadata,
                path,
                label="evaluation cancel marker",
            )
            with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
                try:
                    payload = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise ValueError("evaluation cancel marker is not valid JSON") from exc
            if not isinstance(payload, Mapping) or payload.get("evaluation_id") != evaluation_id:
                raise ValueError("evaluation cancel marker identity mismatch")

            os.rename(path, tombstone)
            moved = True
            moved_metadata = tombstone.lstat()
            if (moved_metadata.st_dev, moved_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                self._restore_replaced_cancel_marker(path, tombstone)
                moved = False
                raise ValueError("evaluation cancel marker changed before deletion")
            _validate_private_file_metadata(
                moved_metadata,
                tombstone,
                label="evaluation cancel marker",
            )
            tombstone.unlink()
            moved = False
        finally:
            os.close(descriptor)
            if moved:
                self._restore_replaced_cancel_marker(path, tombstone)

    @staticmethod
    def _restore_replaced_cancel_marker(path: Path, tombstone: Path) -> None:
        """Best-effort restore without deleting an unverified replacement inode."""

        try:
            os.link(tombstone, path, follow_symlinks=False)
        except (FileExistsError, NotImplementedError, OSError):
            return
        tombstone.unlink(missing_ok=True)

    def _claim_path(self, bot_id: str) -> Path:
        self._verify_private_root()
        digest = sha256(bot_id.encode("utf-8")).hexdigest()[:24]
        return self.root / f".active-{digest}.json"

    def _read_claim(self, bot_id: str) -> dict[str, Any] | None:
        path = self._claim_path(bot_id)
        try:
            exists = self._verify_artifact_file(path, required=False)
        except (PermissionError, ValueError) as exc:
            raise RuntimeError(
                f"Bot {bot_id} evaluation activity claim cannot be a symlink or otherwise unsafe"
            ) from exc
        if not exists:
            return None
        try:
            claim = _read_json(path)
        except (PermissionError, ValueError) as exc:
            raise RuntimeError(f"Bot {bot_id} has an unsafe evaluation activity claim") from exc
        if not claim or claim.get("bot_id") != bot_id:
            raise RuntimeError(f"Bot {bot_id} has an unreadable evaluation activity claim")
        return claim

    def _create_claim(self, bot_id: str, evaluation_id: str) -> None:
        path = self._claim_path(bot_id)
        payload = {
            "bot_id": bot_id,
            "evaluation_id": evaluation_id,
            "owner_pid": os.getpid(),
            "worker_pid": None,
            "created_at": _utc_now(),
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError as exc:
            raise RuntimeError(f"Bot {bot_id} already has an evaluation activity claim") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _update_claim(
        self,
        bot_id: str,
        evaluation_id: str,
        *,
        worker_pid: int,
    ) -> None:
        claim = self._read_claim(bot_id)
        if claim is None or claim.get("evaluation_id") != evaluation_id:
            raise RuntimeError(f"Bot {bot_id} evaluation activity claim changed during startup")
        if claim.get("worker_pid") == worker_pid:
            return
        claim["worker_pid"] = worker_pid
        _write_json(self._claim_path(bot_id), claim)

    def _release_claim(self, bot_id: str, evaluation_id: str) -> None:
        claim = self._read_claim(bot_id)
        if claim is None or claim.get("evaluation_id") != evaluation_id:
            return
        self._claim_path(bot_id).unlink(missing_ok=True)

    def _claim_is_live(
        self,
        claim: Mapping[str, Any],
        directory: Path,
    ) -> bool:
        worker_pid = claim.get("worker_pid")
        if isinstance(worker_pid, int):
            return self._worker_pid_status(worker_pid, directory) != "exited"
        owner_pid = claim.get("owner_pid")
        return isinstance(owner_pid, int) and self._pid_exists(owner_pid)

    def _remove_stale_orphan_claims(self) -> None:
        self._verify_private_root()
        for path in self.root.glob(".active-*.json"):
            self._verify_artifact_file(path, required=True)
            claim = _read_json(path)
            bot_id = str(claim.get("bot_id") or "")
            evaluation_id = str(claim.get("evaluation_id") or "")
            if not bot_id or not evaluation_id:
                raise RuntimeError(f"unreadable evaluation activity claim: {path.name}")
            expected = self._claim_path(bot_id)
            if expected != path:
                raise RuntimeError(f"invalid evaluation activity claim: {path.name}")
            directory = self._evaluation_dir(evaluation_id)
            if not self._claim_is_live(claim, directory):
                self._release_claim(bot_id, evaluation_id)

    def _state(self, evaluation_id: str) -> dict[str, Any]:
        directory = self._verified_evaluation_dir(evaluation_id)
        state = _read_json(directory / "state.json")
        return state

    def _verified_evaluation_dir(self, evaluation_id: str) -> Path:
        directory = self._evaluation_dir(evaluation_id)
        self._verify_evaluation_directory(directory)
        request_path = directory / "request.json"
        state_path = directory / "state.json"
        self._verify_artifact_file(request_path, required=True)
        self._verify_artifact_file(state_path, required=True)
        request = _read_json(request_path)
        state = _read_json(state_path)
        if not request or not state:
            raise KeyError(evaluation_id)
        if request.get("evaluation_id") != evaluation_id:
            raise ValueError("evaluation_id does not match its request record")
        if state.get("evaluation_id") != evaluation_id:
            raise ValueError("evaluation_id does not match its state record")
        return directory

    def _verified_result(
        self,
        evaluation_id: str,
        *,
        directory: Path | None = None,
        required: bool,
    ) -> dict[str, Any]:
        target = directory or self._verified_evaluation_dir(evaluation_id)
        path = target / "result.json"
        exists = self._verify_artifact_file(path, required=required)
        if not exists:
            return {}
        result = _read_json(path)
        if not result:
            raise ValueError("evaluation result is not valid JSON")
        if result.get("evaluation_id") != evaluation_id:
            raise ValueError("evaluation_id does not match its result record")
        return result

    @staticmethod
    def _verify_artifact_file(path: Path, *, required: bool) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if required:
                raise KeyError(path.name)
            return False
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"evaluation artifact cannot be a symlink: {path.name}")
        _validate_private_file_metadata(metadata, path)
        return True

    @staticmethod
    def _verify_evaluation_directory(directory: Path) -> None:
        try:
            metadata = directory.lstat()
        except FileNotFoundError as exc:
            raise KeyError(directory.name) from exc
        _validate_private_directory_metadata(
            metadata,
            directory,
            label="Evaluation directory",
        )

    def _evaluation_dir(self, evaluation_id: str) -> Path:
        if (
            not evaluation_id
            or len(evaluation_id) > 128
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in evaluation_id
            )
        ):
            raise ValueError("invalid evaluation_id")
        self._verify_private_root()
        path = self.root / evaluation_id
        path.relative_to(self.root)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("evaluation directory cannot be a symlink")
        _validate_private_directory_metadata(
            metadata,
            path,
            label="Evaluation directory",
        )
        return path

    @staticmethod
    def _selection_summary(request: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(request.get("kind") or "")
        if kind == "comparison":
            cases = list(request.get("case_refs") or ())
            return {
                "kind": "profile",
                "id": str(request.get("profile_id") or ""),
                "preset": str(request.get("preset") or ""),
                "case_count": len(cases),
            }
        cases = list(request.get("case_ids") or ())
        return {
            "kind": "suite",
            "id": str(request.get("suite_id") or ""),
            "preset": str(request.get("preset") or ""),
            "case_count": len(cases),
        }

    @staticmethod
    def _planned_trial_count(
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> int:
        value = result.get("planned_trials")
        if isinstance(value, int):
            return value
        kind = str(request.get("kind") or "")
        if kind == "comparison":
            return (
                len(request.get("case_refs") or ())
                * int(request.get("repetitions") or 1)
                * len(request.get("target_ids") or request.get("targets") or ())
            )
        return len(request.get("case_ids") or ()) * int(request.get("repetitions") or 1)

    @staticmethod
    def _clone_request(stored: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(stored.get("kind") or "")
        if kind == "comparison":
            request: dict[str, Any] = {
                "kind": "comparison",
                "bot_id": str(stored.get("bot_id") or ""),
                "profile_id": str(stored.get("profile_id") or ""),
                "preset": str(stored.get("preset") or "quick"),
            }
            if request["preset"] == "custom":
                for key in (
                    "target_ids",
                    "case_refs",
                    "repetitions",
                    "max_wall_seconds",
                    "seed",
                ):
                    request[key] = stored.get(key)
            return request
        preset = str(stored.get("preset") or "")
        case_ids = list(stored.get("case_ids") or ())
        if preset and preset != "custom":
            case_ids = []
        return {
            "kind": "suite",
            "bot_id": str(stored.get("bot_id") or ""),
            "suite_id": str(stored.get("suite_id") or ""),
            "case_ids": case_ids,
            "preset": preset,
            "repetitions": int(stored.get("repetitions") or 1),
            "max_wall_seconds": float(stored.get("max_wall_seconds") or 0),
            "seed": int(stored.get("seed") or 0),
            "options": dict(stored.get("options") or {}),
            # External-write approval is scoped to one manual start. A rerun is
            # a new Evaluation and must never inherit the previous approval;
            # product-suite preflight will reject external-write Cases until the
            # operator starts them again through the confirmed form/CLI path.
            "confirm_external_write": False,
            "dry_run": bool(stored.get("dry_run", False)),
            "llm_judge": bool(stored.get("llm_judge", False)),
        }

    @staticmethod
    def _pid_matches_evaluation(pid: int, directory: Path) -> bool:
        if pid <= 0:
            return False
        argv: Sequence[str]
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"(Get-CimInstance Win32_Process -Filter "
                            f'"ProcessId = {pid}").CommandLine'
                        ),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            argv = EvaluationApplication._split_windows_command_line(completed.stdout.strip())
        else:
            try:
                raw_argv = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                return False
            argv = [
                value.decode("utf-8", errors="replace") for value in raw_argv.split(b"\0") if value
            ]
        return EvaluationApplication._argv_matches_evaluation(argv, directory)

    @staticmethod
    def _argv_matches_evaluation(
        argv: Sequence[str],
        directory: Path,
    ) -> bool:
        normalized_tokens = [str(value).casefold() for value in argv]
        managed_entries = [
            index
            for index, value in enumerate(normalized_tokens)
            if value == "chatcopilot.evals.managed_worker"
            and index > 0
            and normalized_tokens[index - 1] == "-m"
        ]
        if len(managed_entries) != 1:
            return False
        output_values: list[str] = []
        index = 0
        while index < len(argv):
            value = str(argv[index])
            if value == "--output":
                if index + 1 >= len(argv):
                    return False
                output_values.append(str(argv[index + 1]))
                index += 2
                continue
            if value.startswith("--output="):
                output_values.append(value.partition("=")[2])
            index += 1
        if len(output_values) != 1:
            return False
        output = Path(output_values[0])
        if not output.is_absolute():
            return False
        try:
            actual = os.path.normcase(str(output.resolve(strict=False)))
            expected = os.path.normcase(str(directory.resolve(strict=False)))
        except OSError:
            return False
        return actual == expected

    @staticmethod
    def _split_windows_command_line(command_line: str) -> Sequence[str]:
        argv: list[str] = []
        length = len(command_line)
        index = 0
        while index < length:
            while index < length and command_line[index] in " \t":
                index += 1
            if index >= length:
                break
            value: list[str] = []
            quoted = False
            while index < length:
                char = command_line[index]
                if char in " \t" and not quoted:
                    break
                if char == "\\":
                    start = index
                    while index < length and command_line[index] == "\\":
                        index += 1
                    slash_count = index - start
                    if index < length and command_line[index] == '"':
                        value.extend("\\" * (slash_count // 2))
                        if slash_count % 2:
                            value.append('"')
                            index += 1
                        elif quoted and index + 1 < length and command_line[index + 1] == '"':
                            value.append('"')
                            index += 2
                        else:
                            quoted = not quoted
                            index += 1
                        continue
                    value.extend("\\" * slash_count)
                    continue
                if char == '"':
                    if quoted and index + 1 < length and command_line[index + 1] == '"':
                        value.append('"')
                        index += 2
                    else:
                        quoted = not quoted
                        index += 1
                    continue
                value.append(char)
                index += 1
            argv.append("".join(value))
            while index < length and command_line[index] in " \t":
                index += 1
        return argv

    @classmethod
    def _worker_pid_status(
        cls,
        pid: int,
        directory: Path,
    ) -> WorkerPidStatus:
        if cls._pid_matches_evaluation(pid, directory):
            return "matched"
        if cls._pid_is_zombie(pid):
            return "exited"
        if cls._pid_exists(pid):
            return "unknown"
        return "exited"

    @staticmethod
    def _pid_is_zombie(pid: int) -> bool:
        if os.name == "nt" or pid <= 0:
            return False
        try:
            value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        close = value.rfind(")")
        return close >= 0 and value[close + 2 : close + 3] == "Z"

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"$p = Get-Process -Id {pid} "
                            "-ErrorAction SilentlyContinue; "
                            "if ($null -eq $p) { 'missing' } "
                            "else { 'present' }"
                        ),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return True
            observation = completed.stdout.strip().lower()
            if observation == "missing":
                return False
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @staticmethod
    def _request_pid_stop(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        try:
            # Cooperative cancellation targets only the managed Core.  It owns
            # the active Trial supervisor and must let that subreaper prove all
            # descendants are gone.  Signalling the worker's whole session can
            # kill a just-spawned supervisor before its cleanup-ready handshake.
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

    @staticmethod
    def _kill_pid(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            return

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0 and process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("evaluation process did not stop after termination") from exc
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("evaluation process did not stop after termination") from exc
        except ProcessLookupError:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("evaluation process identity could not be confirmed") from exc


__all__ = [
    "ACTIVE_STATUSES",
    "EvaluationBlocked",
    "EvaluationApplication",
]
