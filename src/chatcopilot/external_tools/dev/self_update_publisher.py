"""Provider-neutral self-update publisher.

This module owns the source-to-runtime publication contract. Callers pass an
explicit request; no ACP/session contextvars are required here.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from chatcopilot.external_tools.dev.config import get_dev_config
from chatcopilot.external_tools.dev.lifecycle_job import (
    CHANGED_FILES_FILENAME,
    JOBS_DIRNAME,
    REQUEST_FILENAME,
    build_request_payload,
    changed_files,
    make_job_id,
    run_checks,
    source_bot_spec,
    write_json_atomic,
    write_pending_notification,
    write_status,
)
from chatcopilot.project import ENV_PREFIX


@dataclass(frozen=True)
class SelfUpdatePublishRequest:
    reason: str
    source_root: Path
    runtime_root: Path
    bot_spec: Path
    instance_id: str
    workspace_payload: dict[str, Any]
    session_id: str | None = None
    changed_files_override: tuple[str, ...] | None = None
    validation_root: Path | None = None
    validation_bot_spec: Path | None = None
    sync_mode: str = "full"
    expected_hashes: Mapping[str, str | None] | None = None
    prevalidated_checks: tuple[str, ...] | None = None
    audit_context: Mapping[str, str] | None = None


@dataclass(frozen=True)
class SelfUpdatePublishResult:
    job_id: str
    job_dir: Path
    source_root: Path
    runtime_root: Path
    changed_files: tuple[str, ...]
    checks: tuple[str, ...]

    @property
    def summary(self) -> str:
        return (
            "已提交自更新任务。\n"
            f"job_id: {self.job_id}\n"
            f"source_root: {self.source_root}\n"
            f"runtime_root: {self.runtime_root}\n"
            "steps: git diff --check; compileall src tests; botspec validate; "
            "update_instance; service active check; synced file hash check"
        )

    @property
    def outputs(self) -> list[str]:
        return [str(self.job_dir)]


class SelfUpdatePublisher:
    """Validate source changes, write a self-update job, and schedule it."""

    def publish(self, request: SelfUpdatePublishRequest) -> SelfUpdatePublishResult:
        reason = request.reason.strip()
        if not reason:
            raise ValueError("reason is required")

        source_root = request.source_root.expanduser().resolve()
        runtime_root = request.runtime_root.expanduser().resolve()
        bot_spec = request.bot_spec.expanduser().resolve()
        instance_id = request.instance_id.strip()
        workspace_root = _workspace_root(request.workspace_payload)

        if not source_root.is_dir():
            raise RuntimeError(f"{ENV_PREFIX}_DEV_ROOT is not a directory: {source_root}")
        if not runtime_root.is_dir():
            raise RuntimeError(f"{ENV_PREFIX}_RUNTIME_ROOT is not a directory: {runtime_root}")
        if not bot_spec.is_file():
            raise RuntimeError(f"bot spec is not a file: {bot_spec}")
        if not instance_id:
            raise RuntimeError(f"{ENV_PREFIX}_INSTANCE_ID is required")
        if shutil.which("systemd-run") is None:
            raise RuntimeError("systemd-run is required for detached self update")
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemctl is required for service health verification")

        validation_root = (
            request.validation_root.expanduser().resolve()
            if request.validation_root is not None
            else source_root
        )
        validation_bot_spec = (
            request.validation_bot_spec.expanduser().resolve()
            if request.validation_bot_spec is not None
            else bot_spec
        )
        if not validation_root.is_dir():
            raise RuntimeError(f"validation root is not a directory: {validation_root}")
        if not validation_bot_spec.is_file():
            raise RuntimeError(f"validation bot spec is not a file: {validation_bot_spec}")
        if request.expected_hashes is not None:
            _verify_source_hashes(source_root, request.expected_hashes)
        checks = (
            list(request.prevalidated_checks)
            if request.prevalidated_checks is not None
            else run_checks(
                source_root=validation_root,
                bot_spec=validation_bot_spec,
                python_exe=sys.executable,
            )
        )
        files = (
            _normalize_changed_files(request.changed_files_override)
            if request.changed_files_override is not None
            else changed_files(source_root)
        )
        if request.sync_mode not in {"full", "changed_files"}:
            raise ValueError(f"unsupported self-update sync mode: {request.sync_mode}")
        if request.sync_mode == "changed_files" and not files:
            raise ValueError("changed-files sync requires at least one changed file")
        if request.expected_hashes is not None and set(request.expected_hashes) != set(files):
            raise ValueError("expected source hashes must match the changed-files manifest")
        job_id = make_job_id()
        job_dir = workspace_root / JOBS_DIRNAME / job_id
        sync_root: Path | None = None
        manifest: Path | None = None
        if request.sync_mode == "changed_files":
            sync_root, manifest = _prepare_changed_files_overlay(
                source_root=source_root,
                job_dir=job_dir,
                files=files,
                expected_hashes=request.expected_hashes,
            )
            if request.expected_hashes is not None:
                _verify_source_hashes(source_root, request.expected_hashes)
        payload = build_request_payload(
            job_id=job_id,
            reason=reason,
            source_root=source_root,
            runtime_root=runtime_root,
            bot_spec=bot_spec,
            instance_id=instance_id,
            workspace_payload=dict(request.workspace_payload),
            session_id=request.session_id,
            files=files,
            checks=checks,
            sync_mode=request.sync_mode,
            sync_root=sync_root,
            changed_files_manifest=manifest,
            audit_context=dict(request.audit_context or {}),
        )
        write_json_atomic(job_dir / REQUEST_FILENAME, payload)
        write_pending_notification(job_dir, job_id=job_id, session_id=payload["notify"].get("session_id"))
        write_status(job_dir, "queued", "自更新任务已提交，等待 systemd 执行。")
        self._schedule_systemd_job(job_dir=job_dir, source_root=source_root)
        return SelfUpdatePublishResult(
            job_id=job_id,
            job_dir=job_dir,
            source_root=source_root,
            runtime_root=runtime_root,
            changed_files=tuple(files),
            checks=tuple(checks),
        )

    def _schedule_systemd_job(self, *, job_dir: Path, source_root: Path) -> None:
        unit = f"chatcopilot-self-update-{job_dir.name}"
        env = os.environ.copy()
        env["PYTHONPATH"] = (
            f"{source_root / 'src'}{os.pathsep}{env['PYTHONPATH']}"
            if env.get("PYTHONPATH")
            else str(source_root / "src")
        )
        cmd = [
            "systemd-run",
            "--user",
            "--collect",
            f"--unit={unit}",
            f"--property=WorkingDirectory={source_root}",
            f"--setenv=PYTHONPATH={env['PYTHONPATH']}",
            sys.executable,
            "-m",
            "chatcopilot.external_tools.dev.lifecycle_worker",
            str(job_dir),
        ]
        result = subprocess.run(cmd, cwd=str(source_root), capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            detail = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
            raise RuntimeError(f"systemd-run failed: {detail.strip()}")


def build_publish_request_from_env(
    *,
    reason: str,
    workspace_payload: dict[str, Any],
    session_id: str | None = None,
    changed_files_override: tuple[str, ...] | None = None,
    validation_root: Path | None = None,
    validation_bot_spec: Path | None = None,
    sync_mode: str = "full",
    expected_hashes: Mapping[str, str | None] | None = None,
    prevalidated_checks: tuple[str, ...] | None = None,
    audit_context: Mapping[str, str] | None = None,
) -> SelfUpdatePublishRequest:
    config = get_dev_config(force_reload=True)
    source_root = config.repo_root
    runtime_root = _runtime_root()
    instance_id = os.environ.get(f"{ENV_PREFIX}_INSTANCE_ID", "").strip()
    if not instance_id:
        raise RuntimeError(f"{ENV_PREFIX}_INSTANCE_ID is required")
    return SelfUpdatePublishRequest(
        reason=reason,
        source_root=source_root,
        runtime_root=runtime_root,
        bot_spec=source_bot_spec(source_root, instance_id),
        instance_id=instance_id,
        workspace_payload=dict(workspace_payload),
        session_id=session_id or os.environ.get(f"{ENV_PREFIX}_SESSION_ID", "").strip() or None,
        changed_files_override=changed_files_override,
        validation_root=validation_root,
        validation_bot_spec=validation_bot_spec,
        sync_mode=sync_mode,
        expected_hashes=expected_hashes,
        prevalidated_checks=prevalidated_checks,
        audit_context=audit_context,
    )


def publish_self_update(request: SelfUpdatePublishRequest) -> SelfUpdatePublishResult:
    return SelfUpdatePublisher().publish(request)


def _verify_source_hashes(
    source_root: str | Path,
    expected_hashes: Mapping[str, str | None],
) -> None:
    root = Path(source_root).expanduser().resolve()
    for rel, expected in sorted(expected_hashes.items()):
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"source hash path escapes root: {rel}") from exc
        if expected is None:
            if candidate.exists() or candidate.is_symlink():
                raise RuntimeError(f"source changed after validation: {rel} was recreated")
            continue
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(
                f"source changed after validation: {rel} is not a regular file"
            )
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError(f"source changed after validation: {rel} hash drifted")


def _runtime_root() -> Path:
    raw = os.environ.get(f"{ENV_PREFIX}_RUNTIME_ROOT", "").strip()
    if not raw:
        raw = os.environ.get(f"{ENV_PREFIX}_HOME", "").strip()
    if not raw:
        raise RuntimeError(f"{ENV_PREFIX}_RUNTIME_ROOT or {ENV_PREFIX}_HOME is required")
    return Path(raw).expanduser().resolve()


def _workspace_root(payload: dict[str, Any]) -> Path:
    raw = str(payload.get("root") or "").strip()
    if not raw:
        raise RuntimeError("workspace_payload.root is required")
    return Path(raw).expanduser().resolve()


def _normalize_changed_files(files: tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for raw in files:
        rel = str(raw or "").strip().replace("\\", "/")
        path = PurePosixPath(rel)
        if not rel or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"invalid changed file path: {raw}")
        value = path.as_posix()
        if value not in normalized:
            normalized.append(value)
    return normalized


def _prepare_changed_files_overlay(
    *,
    source_root: Path,
    job_dir: Path,
    files: list[str],
    expected_hashes: Mapping[str, str | None] | None = None,
) -> tuple[Path, Path]:
    overlay = job_dir / "source-overlay"
    overlay.mkdir(parents=True, exist_ok=False)
    for rel in files:
        candidate = source_root / rel
        if candidate.is_symlink():
            raise RuntimeError(f"changed-files sync does not accept symlinks: {rel}")
        source = candidate.resolve()
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise RuntimeError(f"changed file escapes source root: {rel}") from exc
        if not source.exists():
            continue
        if not source.is_file():
            raise RuntimeError(f"changed-files sync supports regular files only: {rel}")
        target = overlay / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if expected_hashes is not None:
        _verify_source_hashes(overlay, expected_hashes)
    manifest = job_dir / CHANGED_FILES_FILENAME
    manifest.write_text("".join(f"{rel}\n" for rel in files), encoding="utf-8")
    return overlay, manifest


__all__ = [
    "SelfUpdatePublishRequest",
    "SelfUpdatePublishResult",
    "SelfUpdatePublisher",
    "build_publish_request_from_env",
    "publish_self_update",
]
