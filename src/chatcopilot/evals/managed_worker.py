"""Managed Evaluation worker owned by the local Evaluation service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import sys
import threading
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, TextIO

from chatcopilot.core.logging import configure_logging
from chatcopilot.evals.evaluations import (
    EvaluationValidationError,
    run_evaluation,
)
from chatcopilot.evals.redaction import collect_env_secrets, sanitize_text


class _SanitizedWriter:
    def __init__(
        self,
        handle: TextIO,
        *,
        secrets: tuple[str, ...],
        roots: dict[str, Path],
    ) -> None:
        self._handle = handle
        self._secrets = secrets
        self._roots = roots
        self.encoding = "utf-8"

    def write(self, value: str) -> int:
        clean = sanitize_text(
            str(value),
            secrets=self._secrets,
            roots=self._roots,
        )
        self._handle.write(clean)
        return len(value)

    def flush(self) -> None:
        self._handle.flush()

    def isatty(self) -> bool:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatcopilot.evals.managed_worker")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cancel-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--startup-fd", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        output, request_path, cancel_path, log_path = _validated_paths(args)
        outer_request = _read_request(request_path)
        core_request = outer_request.get("core_request")
        if not isinstance(core_request, dict):
            raise ValueError("managed request has no core_request object")
        evaluation_id = str(outer_request.get("evaluation_id") or "")
        if not evaluation_id or evaluation_id != output.name:
            raise ValueError("managed request evaluation_id does not match output")
        if core_request.get("evaluation_id") != evaluation_id:
            raise ValueError("managed core_request evaluation_id does not match output")
        _await_startup(args.startup_fd)
        _verify_bot_spec_snapshot(outer_request)
        claim_path = _managed_claim_path(output, outer_request)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"managed Evaluation bootstrap failed: {exc}\n")
        return 2

    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        _validate_private_file(metadata, log_path, label="managed run.log")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"managed Evaluation log bootstrap failed: {exc}\n")
        return 2
    if os.name != "nt":
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8", buffering=1) as handle:
        writer = _SanitizedWriter(
            handle,
            secrets=tuple(collect_env_secrets()),
            roots={"evaluation": output, "repository": Path.cwd().resolve()},
        )
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(writer))
            stack.enter_context(redirect_stderr(writer))
            return _run_managed(
                core_request,
                output=output,
                cancel_path=cancel_path,
                claim_path=claim_path,
                evaluation_id=evaluation_id,
            )


def _verify_bot_spec_snapshot(request: dict[str, Any]) -> None:
    relative = str(request.get("bot_spec") or "").strip()
    expected = str(request.get("bot_spec_sha256") or "").strip().lower()
    if not relative or Path(relative).is_absolute() or "\\" in relative:
        raise ValueError("managed request has an invalid BotSpec snapshot path")
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("managed request has an invalid BotSpec snapshot digest")
    repository = Path.cwd().resolve()
    candidate = repository.joinpath(*Path(relative).parts)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(repository)
    except ValueError as exc:
        raise ValueError("managed BotSpec snapshot escapes repository") from exc
    metadata = resolved.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or resolved.is_symlink():
        raise ValueError("managed BotSpec snapshot is not a regular file")
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("BotSpec changed after Evaluation preflight")


def _run_managed(
    request: dict[str, Any],
    *,
    output: Path,
    cancel_path: Path,
    claim_path: Path,
    evaluation_id: str,
) -> int:
    configure_logging("INFO", "CHATCOPILOT_EVAL_LOG_LEVEL")
    cancelled = threading.Event()

    def request_cancel(_signum: int, _frame: Any) -> None:
        cancelled.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_cancel)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_cancel)

    def cancel_check() -> bool:
        if cancelled.is_set():
            return True
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(cancel_path, flags)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError("managed cancel marker cannot be opened safely") from exc
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            _validate_private_file(
                metadata,
                cancel_path,
                label="managed cancel marker",
            )
            payload = json.load(handle)
        if not isinstance(payload, dict) or payload.get("evaluation_id") != evaluation_id:
            raise ValueError("managed cancel marker identity mismatch")
        return True

    def progress(payload: dict[str, Any]) -> None:
        print(
            "__EVAL_EVENT__ " + json.dumps(payload, ensure_ascii=False),
            flush=True,
        )

    try:
        result = run_evaluation(
            request,
            output=output,
            progress_callback=progress,
            cancel_check=cancel_check,
            managed=True,
            authority_claim_path=claim_path,
        )
    except EvaluationValidationError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - managed process boundary
        print(f"managed Evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0 if result.status in {"completed", "cancelled"} else 1


def _managed_claim_path(output: Path, request: dict[str, Any]) -> Path:
    """Prove the startup gate persisted this worker in state and claim."""

    bot_id = str(request.get("bot_id") or "").strip()
    if not bot_id:
        raise ValueError("managed request has no bot_id for its activity claim")
    digest = hashlib.sha256(bot_id.encode("utf-8")).hexdigest()[:24]
    path = output.parent / f".active-{digest}.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("managed Evaluation activity claim is unavailable or unsafe")
    claim = _read_private_json_object(path, label="managed activity claim")
    expected_claim_fields = {
        "bot_id",
        "evaluation_id",
        "owner_pid",
        "worker_pid",
        "created_at",
    }
    if set(claim) != expected_claim_fields:
        raise ValueError("managed Evaluation activity claim fields are invalid")
    if (
        claim.get("bot_id") != bot_id
        or claim.get("evaluation_id") != output.name
        or isinstance(claim.get("worker_pid"), bool)
        or not isinstance(claim.get("worker_pid"), int)
        or claim.get("worker_pid") != os.getpid()
        or isinstance(claim.get("owner_pid"), bool)
        or not isinstance(claim.get("owner_pid"), int)
        or int(claim["owner_pid"]) <= 0
        or not isinstance(claim.get("created_at"), str)
        or not str(claim["created_at"]).strip()
    ):
        raise ValueError("managed Evaluation activity claim identity is invalid")

    state = _read_private_json_object(output / "state.json", label="managed state")
    if (
        state.get("evaluation_id") != output.name
        or state.get("kind") != request.get("kind")
        or state.get("status") != "running"
        or isinstance(state.get("pid"), bool)
        or not isinstance(state.get("pid"), int)
        or state.get("pid") != os.getpid()
        or not isinstance(state.get("started_at"), str)
        or not str(state["started_at"]).strip()
    ):
        raise ValueError("managed Evaluation state does not prove the current worker identity")
    return path


def _await_startup(descriptor: int) -> None:
    """Wait until lifecycle state and the activity claim contain this worker PID."""

    if descriptor < 0:
        raise ValueError("managed startup descriptor is invalid")
    try:
        value = os.read(descriptor, 1)
    finally:
        os.close(descriptor)
    if value != b"\x01":
        raise ValueError("managed startup handshake was not released")


def _validated_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    raw_output = Path(args.output).expanduser()
    if not raw_output.is_absolute() or raw_output.is_symlink():
        raise ValueError("managed output must be an absolute real directory")
    output = raw_output.resolve()
    if not output.is_dir():
        raise ValueError("managed output directory does not exist")
    output_metadata = output.lstat()
    if not stat.S_ISDIR(output_metadata.st_mode):
        raise ValueError("managed output must be a real directory")
    if os.name != "nt":
        if output_metadata.st_uid != os.getuid():
            raise PermissionError("managed output must be owned by the service user")
        if stat.S_IMODE(output_metadata.st_mode) != 0o700:
            raise PermissionError("managed output must use mode 0700")
    expected = {
        "request": output / "request.json",
        "cancel": output / ".cancel-requested.json",
        "log": output / "run.log",
    }
    provided = {
        "request": Path(args.request).expanduser(),
        "cancel": Path(args.cancel_file).expanduser(),
        "log": Path(args.log_file).expanduser(),
    }
    for name, path in provided.items():
        if not path.is_absolute() or path.is_symlink():
            raise ValueError(f"managed {name} path must be absolute and not a symlink")
        if path.resolve(strict=False) != expected[name]:
            raise ValueError(f"managed {name} path does not match output")
    if not expected["request"].is_file():
        raise ValueError("managed request.json does not exist")
    if expected["log"].exists() and not expected["log"].is_file():
        raise ValueError("managed run.log must be a regular file")
    return output, expected["request"], expected["cancel"], expected["log"]


def _read_request(path: Path) -> dict[str, Any]:
    return _read_private_json_object(path, label="managed request")


def _read_private_json_object(path: Path, *, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely") from exc
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        _validate_private_file(metadata, path, label=label)
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return {str(key): value for key, value in payload.items()}


def _validate_private_file(
    metadata: os.stat_result,
    path: Path,
    *,
    label: str,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} must be owned by the service user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"{label} must use mode 0600")
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} must have exactly one hard link: {path.name}")


if __name__ == "__main__":
    raise SystemExit(main())
