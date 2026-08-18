"""Detached source-to-runtime update job helpers for dev lifecycle tools."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from chatcopilot.core.jobs import (
    read_json_file as _read_core_json_file,
    write_json_atomic as _write_core_json_atomic,
)
from chatcopilot.project import ENV_PREFIX

JOBS_DIRNAME = "jobs"
NOTIFICATION_FILENAME = "notification.json"
REQUEST_FILENAME = "request.json"
RESULT_FILENAME = "result.json"
STATUS_FILENAME = "status.json"
STDOUT_FILENAME = "stdout.log"
STDERR_FILENAME = "stderr.log"
CHANGED_FILES_FILENAME = "changed_files.txt"


def make_job_id() -> str:
    return f"job_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_core_json_atomic(path, payload)


def write_status(job_dir: Path, status: str, message: str) -> None:
    write_json_atomic(
        job_dir / STATUS_FILENAME,
        {"status": status, "message": message, "updated_at": time.time()},
    )


def write_pending_notification(job_dir: Path, *, job_id: str, session_id: str | None) -> None:
    write_json_atomic(
        job_dir / NOTIFICATION_FILENAME,
        {
            "job_id": job_id,
            "session_id": session_id,
            "delivery": "pending",
            "channel": "chat_platform",
            "attempts": 0,
            "last_error": "",
            "updated_at": time.time(),
            "delivered_at": None,
            "receive_id_type": None,
            "receive_id": None,
            "message_id": None,
        },
    )


def run_checks(*, source_root: Path, bot_spec: Path, python_exe: str) -> list[str]:
    commands = [
        ["git", "diff", "--check"],
        [python_exe, "-m", "compileall", "-q", "src", "tests"],
        [python_exe, "-m", "chatcopilot", "botspec", "validate", str(bot_spec)],
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{source_root / 'src'}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(source_root / "src")
    summaries: list[str] = []
    for cmd in commands:
        result = subprocess.run(
            cmd,
            cwd=str(source_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        rendered = " ".join(cmd)
        if result.returncode != 0:
            detail = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
            raise RuntimeError(f"preflight failed: {rendered}\n{detail.strip()}")
        summaries.append(rendered)
    return summaries


def changed_files(source_root: Path) -> list[str]:
    tracked = _run_git_lines(source_root, ["git", "diff", "--name-only", "HEAD"])
    untracked = _run_git_lines(source_root, ["git", "ls-files", "--others", "--exclude-standard"])
    out: list[str] = []
    for rel in (*tracked, *untracked):
        normalized = rel.strip().replace("\\", "/")
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _run_git_lines(cwd: Path, cmd: list[str]) -> list[str]:
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return [line for line in result.stdout.splitlines() if line.strip()]


def build_request_payload(
    *,
    job_id: str,
    reason: str,
    source_root: Path,
    runtime_root: Path,
    bot_spec: Path,
    instance_id: str,
    workspace_payload: dict[str, Any],
    session_id: str | None,
    files: list[str],
    checks: list[str],
    sync_mode: str = "full",
    sync_root: Path | None = None,
    changed_files_manifest: Path | None = None,
    audit_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "tool_name": "finalize_self_update",
        "execution_policy": "detached_systemd",
        "queue_name": "self_update",
        "submitted_at": time.time(),
        "audit": dict(audit_context or {}),
        "workspace": workspace_payload,
        "notify": {**workspace_payload, "session_id": session_id},
        "args": {
            "reason": reason,
            "source_root": str(source_root),
            "runtime_root": str(runtime_root),
            "bot_spec": str(bot_spec),
            "instance_id": instance_id,
            "changed_files": files,
            "checks": checks,
            "python_exe": sys.executable,
            "sync_mode": sync_mode,
            "sync_root": str(sync_root) if sync_root is not None else str(source_root),
            "changed_files_manifest": (
                str(changed_files_manifest) if changed_files_manifest is not None else ""
            ),
        },
    }


def run_detached_job(job_dir: Path) -> int:
    started = time.time()
    if job_dir.parent.name != JOBS_DIRNAME or not job_dir.name.startswith("job_"):
        return 2
    request = _read_core_json_file(job_dir / REQUEST_FILENAME)
    if not isinstance(request, dict):
        return 2
    if (
        str(request.get("job_id") or "") != job_dir.name
        or str(request.get("tool_name") or "") != "finalize_self_update"
        or str(request.get("execution_policy") or "") != "detached_systemd"
        or str(request.get("queue_name") or "") != "self_update"
    ):
        return 2
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    source_root_raw = str(args.get("source_root") or "").strip()
    runtime_root_raw = str(args.get("runtime_root") or "").strip()
    instance_id = str(args.get("instance_id") or "").strip()
    if not source_root_raw or not runtime_root_raw or not instance_id:
        return 2
    source_root = Path(source_root_raw).expanduser().resolve()
    runtime_root = Path(runtime_root_raw).expanduser().resolve()
    changed = [str(item) for item in (args.get("changed_files") or []) if str(item).strip()]
    sync_mode = str(args.get("sync_mode") or "full").strip()
    sync_root = Path(str(args.get("sync_root") or source_root)).expanduser().resolve()
    changed_manifest = str(args.get("changed_files_manifest") or "").strip()

    stdout_path = job_dir / STDOUT_FILENAME
    stderr_path = job_dir / STDERR_FILENAME
    try:
        write_status(job_dir, "running", "自更新任务正在同步运行实例并重启机器人。")
        _append(stdout_path, f"source={source_root}\nruntime={runtime_root}\ninstance={instance_id}\n")
        update_script = source_root / "deploy" / "wsl" / "update_instance.sh"
        if not update_script.is_file():
            raise RuntimeError(f"update script not found: {update_script}")
        cmd = ["bash", str(update_script), "--instance", instance_id, "--src", str(source_root), "--dst", str(runtime_root)]
        if sync_mode == "changed_files":
            if not changed_manifest:
                raise RuntimeError("changed-files sync requires a manifest")
            cmd.extend(
                [
                    "--sync-src",
                    str(sync_root),
                    "--changed-files",
                    changed_manifest,
                ]
            )
        elif sync_mode != "full":
            raise RuntimeError(f"unsupported self-update sync mode: {sync_mode}")
        result = subprocess.run(cmd, cwd=str(source_root), capture_output=True, text=True, timeout=600)
        _append(stdout_path, result.stdout)
        _append(stderr_path, result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"update_instance failed with exit code {result.returncode}")
        _verify_service(instance_id)
        verification_root = sync_root if sync_mode == "changed_files" else source_root
        verified = _verify_synced_files(verification_root, runtime_root, changed)
        summary = (
            "自更新完成：源仓已同步到运行实例，配置已重渲染，机器人服务已重启。"
            f"\ninstance: {instance_id}\nverified_files: {len(verified)}"
        )
        payload = {
            "job_id": request.get("job_id"),
            "tool_name": "finalize_self_update",
            "ok": True,
            "summary": summary,
            "outputs": [str(job_dir)],
            "error": "",
            "console_tail": _tail(stdout_path, stderr_path),
            "started_at": started,
            "finished_at": time.time(),
        }
        write_json_atomic(job_dir / RESULT_FILENAME, payload)
        write_status(job_dir, "succeeded", summary)
        return 0
    except Exception as exc:  # noqa: BLE001
        _append(stderr_path, f"\n{type(exc).__name__}: {exc}\n")
        payload = {
            "job_id": request.get("job_id"),
            "tool_name": "finalize_self_update",
            "ok": False,
            "summary": "",
            "outputs": [],
            "error": f"{type(exc).__name__}: {exc}",
            "console_tail": _tail(stdout_path, stderr_path),
            "started_at": started,
            "finished_at": time.time(),
        }
        write_json_atomic(job_dir / RESULT_FILENAME, payload)
        write_status(job_dir, "failed", payload["error"])
        return 1


def _verify_service(instance_id: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", f"chatcopilot@{instance_id}.service"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or result.stdout.strip() != "active":
        raise RuntimeError(f"service is not active: chatcopilot@{instance_id}.service")


def _verify_synced_files(source_root: Path, runtime_root: Path, files: list[str]) -> list[str]:
    verified: list[str] = []
    for rel in files:
        source = (source_root / rel).resolve()
        target = (runtime_root / rel).resolve()
        if not source.exists():
            if target.exists():
                raise RuntimeError(f"deleted file still exists in runtime copy: {rel}")
            verified.append(rel)
            continue
        if not source.is_file():
            continue
        if not target.is_file():
            raise RuntimeError(f"synced file missing in runtime copy: {rel}")
        if _sha256(source) != _sha256(target):
            raise RuntimeError(f"synced file differs in runtime copy: {rel}")
        verified.append(rel)
    return verified


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _append(path: Path, text: str) -> None:
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)


def _tail(*paths: Path, limit: int = 4000) -> str:
    text = ""
    for path in paths:
        if path.is_file():
            text += path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def workspace_payload(workspace: Any) -> dict[str, Any]:
    return {
        "root": str(workspace.root),
        "chat_kind": getattr(workspace, "chat_kind", None),
        "chat_id": getattr(workspace, "chat_id", None),
        "user_id": getattr(workspace, "user_id", None),
        "user_name": getattr(workspace, "user_name", None),
    }


def source_bot_spec(source_root: Path, instance_id: str) -> Path:
    override = os.environ.get(f"{ENV_PREFIX}_SOURCE_BOT_SPEC", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (source_root / "bots" / instance_id / "bot.yaml").resolve()
