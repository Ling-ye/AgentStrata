"""Persistent per-instance recovery loop for isolated code tasks."""
from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path
from typing import Any

from chatcopilot.contracts.code_tasks import (
    CODE_TASK_ACTIVE_STATUSES,
    CODE_TASK_TOOL,
)
from chatcopilot.core.jobs import (
    iter_job_request_paths,
    read_json_file,
    write_job_status,
    write_json_atomic,
)
from chatcopilot.external_tools.dev import code_task_runtime as runtime
from chatcopilot.project import ENV_PREFIX

_STOP = False


def recover_code_tasks_once(workspace_root: Path) -> dict[str, int]:
    instance_id = os.environ.get(f"{ENV_PREFIX}_INSTANCE_ID", "").strip()
    if not instance_id:
        raise RuntimeError("code-task recovery requires CHATCOPILOT_INSTANCE_ID")
    counts = {
        "scanned": 0,
        "skipped_instance": 0,
        "dispatched": 0,
        "interrupted": 0,
        "cancelled": 0,
        "errors": 0,
    }
    for request_path in iter_job_request_paths(workspace_root):
        request = read_json_file(request_path) or {}
        if str(request.get("tool_name") or "") != CODE_TASK_TOOL:
            continue
        counts["scanned"] += 1
        if str(request.get("instance_id") or "") != instance_id:
            counts["skipped_instance"] += 1
            continue
        job_dir = request_path.parent
        status = read_json_file(job_dir / "status.json") or {}
        state = str(status.get("status") or "")
        if state not in CODE_TASK_ACTIVE_STATUSES:
            continue
        if runtime.code_task_dispatch_active(job_dir):
            continue
        if state == "queued":
            try:
                runtime.schedule_code_task_worker(request_path)
                counts["dispatched"] += 1
            except Exception as exc:  # noqa: BLE001
                _finish_recovery(
                    request,
                    job_dir,
                    status="failed",
                    error_code="code_task_dispatch_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                counts["errors"] += 1
            continue
        if state == "cancel_requested":
            _finish_recovery(
                request,
                job_dir,
                status="cancelled",
                error_code="code_task_cancelled",
                error="Code task was cancelled before recovery.",
            )
            counts["cancelled"] += 1
            continue
        _finish_recovery(
            request,
            job_dir,
            status="interrupted",
            error_code="code_task_interrupted",
            error="Code task was interrupted by a system restart; Owner resume is required.",
        )
        counts["interrupted"] += 1
    return counts


def _finish_recovery(
    request: dict[str, Any],
    job_dir: Path,
    *,
    status: str,
    error_code: str,
    error: str,
) -> None:
    now = time.time()
    write_job_status(
        job_dir,
        status,
        error,
        stage=status,
        error_code=error_code,
    )
    write_json_atomic(
        job_dir / "result.json",
        {
            "job_id": str(request.get("job_id") or job_dir.name),
            "tool_name": CODE_TASK_TOOL,
            "ok": False,
            "summary": "",
            "outputs": [str(job_dir)],
            "error": error,
            "error_code": error_code,
            "details": {"failed_stage": status},
            "stage": status,
            "console_tail": "",
            "started_at": now,
            "finished_at": now,
        },
    )
    attempts = request.get("attempts")
    if isinstance(attempts, list):
        attempt = int((read_json_file(job_dir / "status.json") or {}).get("attempt") or 1)
        for item in attempts:
            if not isinstance(item, dict) or int(item.get("number") or 0) != attempt:
                continue
            item.update(
                {
                    "status": status,
                    "finished_at": now,
                    "error_code": error_code,
                    "error": error,
                }
            )
            break
        write_json_atomic(job_dir / "request.json", request)


def run_service(
    workspace_root: Path,
    *,
    poll_seconds: float = 5.0,
    once: bool = False,
) -> int:
    global _STOP
    _STOP = False
    last_cleanup = 0.0
    while not _STOP:
        recover_code_tasks_once(workspace_root)
        now = time.monotonic()
        if now - last_cleanup >= 300:
            runtime.cleanup_code_task_retention(workspace_root)
            last_cleanup = now
        if once:
            return 0
        time.sleep(max(1.0, poll_seconds))
    return 0


def _request_stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    root = args.workspace_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return run_service(
        root,
        poll_seconds=args.poll_seconds,
        once=args.once,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "recover_code_tasks_once", "run_service"]
