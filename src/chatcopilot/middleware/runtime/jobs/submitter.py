"""后台任务提交：BackgroundJob 元数据 + 启动 worker 子进程 + 状态查询。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from chatcopilot.contracts.code_tasks import (
    CODE_TASK_TOOL,
    validate_code_task_title,
)
from chatcopilot.core.jobs import (
    BackgroundJob,
    find_job,
    is_job_completed,
    job_storage_root,
    job_notification_workspace,
    list_unnotified_completed_jobs,
    queue_position,
    read_job_result,
    read_job_status,
    safe_segment,
    write_job_status as _write_job_status,
)
from chatcopilot.contracts.tools import (
    EXECUTION_GLOBAL_SERIAL_BACKGROUND,
    EXECUTION_USER_SERIAL_BACKGROUND,
)
from chatcopilot.middleware.runtime.jobs.notification import (
    NOTIFICATION_FILENAME,
    notification_state_payload,
    read_json_file,
    write_json_atomic,
)
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.project import ENV_PREFIX

_RESULT_FILENAME = "result.json"
_REQUEST_FILENAME = "request.json"
_STATUS_FILENAME = "status.json"
_STDOUT_FILENAME = "stdout.log"
_STDERR_FILENAME = "stderr.log"
def submit_tool_job(
    *,
    tool_name: str,
    args: Dict[str, Any],
    execution_policy: str,
    workspace: Workspace,
    session_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> BackgroundJob:
    """创建后台任务并启动一个等待队列的 worker 子进程。"""
    if execution_policy not in {
        EXECUTION_GLOBAL_SERIAL_BACKGROUND,
        EXECUTION_USER_SERIAL_BACKGROUND,
    }:
        raise ValueError(f"不支持的后台执行策略: {execution_policy}")
    instance_id = ""
    if tool_name == CODE_TASK_TOOL:
        instance_id = os.environ.get(f"{ENV_PREFIX}_INSTANCE_ID", "").strip()
        if not instance_id:
            raise ValueError("start_code_task requires CHATCOPILOT_INSTANCE_ID")
        prompt = str((args or {}).get("prompt") or "").strip()
        if not prompt:
            raise ValueError("start_code_task requires prompt")
        title = validate_code_task_title(str((args or {}).get("title") or ""))
        args = {**(args or {}), "prompt": prompt, "title": title}

    job_id = f"job_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    job_dir = job_storage_root(workspace, create=True) / job_id
    job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    job_dir.chmod(0o700)

    queue_name = _queue_name(execution_policy, workspace, tool_name=tool_name)
    request_path = job_dir / _REQUEST_FILENAME
    result_path = job_dir / _RESULT_FILENAME
    request = {
        "job_id": job_id,
        "tool_name": tool_name,
        "args": args or {},
        "code_job_contract": (
            dict(args.get("contract") or {})
            if isinstance(args.get("contract"), dict)
            else None
        ),
        "execution_profile": (
            dict(args.get("execution_profile") or {})
            if isinstance(args.get("execution_profile"), dict)
            else None
        ),
        "execution_policy": execution_policy,
        "queue_name": queue_name,
        "submitted_at": time.time(),
        "workspace": _workspace_payload(workspace),
        "notify": _notification_request_payload(workspace, session_id=session_id),
        "trace_id": trace_id,
        "task_id": trace_id if str(trace_id or "").startswith("task_") else None,
    }
    if tool_name == CODE_TASK_TOOL:
        request["instance_id"] = instance_id
        request["attempts"] = [
            {
                "number": 1,
                "prompt": str(args["prompt"]),
                "title": str(args["title"]),
                "submitted_at": request["submitted_at"],
                "requested_by": workspace.user_id,
                "status": "queued",
            }
        ]
    write_json_atomic(request_path, request)
    write_json_atomic(
        job_dir / NOTIFICATION_FILENAME,
        notification_state_payload(
            job_id=job_id,
            session_id=session_id,
            delivery="pending",
            attempts=0,
            channel="chat_platform",
        ),
    )
    write_status(job_dir, "queued", "任务已提交，等待执行。", stage="queued")
    _spawn_worker(job_dir, request_path, workspace)

    return BackgroundJob(
        job_id=job_id,
        tool_name=tool_name,
        execution_policy=execution_policy,
        job_dir=job_dir,
        request_path=request_path,
        result_path=result_path,
        session_id=session_id,
        user_id=workspace.user_id,
        queue_name=queue_name,
        queue_position=queue_position(queue_name, job_id),
    )


def write_status(
    job_dir: Path,
    status: str,
    message: str,
    *,
    stage: str = "",
    error_code: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    _write_job_status(
        job_dir,
        status,
        message,
        stage=stage,
        error_code=error_code,
        details=dict(details or {}),
    )


# ----------------------------------------------------------------------------
# 内部 helper
# ----------------------------------------------------------------------------
def _spawn_worker(job_dir: Path, request_path: Path, workspace: Workspace) -> None:
    request = read_json_file(request_path) or {}
    if str(request.get("tool_name") or "") == CODE_TASK_TOOL:
        from chatcopilot.external_tools.dev.code_task_runtime import (
            schedule_code_task_worker,
        )

        schedule_code_task_worker(request_path)
        return
    env = os.environ.copy()
    env.update(workspace_env(_workspace_payload(workspace)))
    cmd = [sys.executable, "-m", "chatcopilot.middleware.runtime.jobs.worker", str(request_path)]
    stdout = (job_dir / _STDOUT_FILENAME).open("w", encoding="utf-8")
    stderr = (job_dir / _STDERR_FILENAME).open("w", encoding="utf-8")
    try:
        subprocess.Popen(  # noqa: S603 - controlled module invocation with current interpreter
            cmd,
            cwd=str(Path.cwd()),
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    finally:
        stdout.close()
        stderr.close()


def _workspace_payload(workspace: Workspace) -> Dict[str, Any]:
    return {
        "root": str(workspace.root),
        "chat_kind": workspace.chat_kind,
        "chat_id": workspace.chat_id,
        "user_id": workspace.user_id,
        "user_name": workspace.user_name,
        "scope": workspace.scope,
    }


def _notification_request_payload(
    workspace: Workspace,
    *,
    session_id: Optional[str],
) -> Dict[str, Any]:
    payload = _workspace_payload(workspace)
    payload["session_id"] = session_id
    return payload


def workspace_env(payload: Dict[str, Any]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if payload.get("root"):
        env[f"{ENV_PREFIX}_WORKSPACE"] = str(payload["root"])
    for key, env_name in (
        ("chat_kind", f"{ENV_PREFIX}_CHAT_KIND"),
        ("chat_id", f"{ENV_PREFIX}_CHAT_ID"),
        ("user_id", f"{ENV_PREFIX}_USER_ID"),
        ("user_name", f"{ENV_PREFIX}_USER_NAME"),
        ("scope", f"{ENV_PREFIX}_WORKSPACE_SCOPE"),
    ):
        value = payload.get(key)
        if value:
            env[env_name] = str(value)
    return env


def _queue_name(
    policy: str,
    workspace: Workspace,
    *,
    tool_name: str = "",
) -> str:
    if tool_name == CODE_TASK_TOOL:
        return "code_tasks_global"
    if policy == EXECUTION_GLOBAL_SERIAL_BACKGROUND:
        return "datasource_global"
    if policy == EXECUTION_USER_SERIAL_BACKGROUND:
        user = workspace.user_id or workspace.chat_id or workspace.root.name
        return f"analysis_user_{safe_segment(user)}"
    return "default"


def queue_capacity(policy: str) -> int:
    if policy == EXECUTION_USER_SERIAL_BACKGROUND:
        return _coerce_int(os.environ.get(f"{ENV_PREFIX}_ANALYSIS_USER_CONCURRENCY"), 1)
    return 1


def _coerce_int(raw: object, fallback: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(1, value)


__all__ = [
    "BackgroundJob",
    "find_job",
    "is_job_completed",
    "job_notification_workspace",
    "list_unnotified_completed_jobs",
    "queue_capacity",
    "read_job_result",
    "read_job_status",
    "submit_tool_job",
    "workspace_env",
    "write_status",
]
