"""Owner-only lifecycle tools for isolated asynchronous source development."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from chatcopilot.contracts.code_tasks import (
    CODE_TASK_RESUMABLE_STATUSES,
    validate_code_task_title,
)
from chatcopilot.core.jobs import (
    BackgroundJob,
    append_code_task_attempt,
    find_job,
    latest_code_job,
    read_job_status,
    request_job_cancel,
)
from chatcopilot.external_tools.dev.code_task_runtime import (
    code_task_limits,
    execute_code_task,
    schedule_code_task_worker,
    terminate_recorded_task,
)
from chatcopilot.external_tools.dev.code_task_delivery import (
    delivery_retry_pending,
)
from chatcopilot.external_tools.shared.spec_helpers import schema_property
from chatcopilot.external_tools.shared.tool_spec import (
    EXECUTION_GLOBAL_SERIAL_BACKGROUND,
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)

_OWNER = "dev.code_tasks"


def _tool(**kwargs: Any) -> ToolDef:
    return ToolDef(
        owner=_OWNER,
        module=__name__,
        requires_role="owner",
        metadata={"tags": ["code", "owner", "isolated"]},
        **kwargs,
    )


def _start(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return execute_code_task(args, ctx)


def _status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    job = _resolve_job(args, ctx)
    payload = _public_status(job)
    return ToolResult(ok=True, summary="已查询代码任务状态。", data=payload)


def _cancel(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    job = _resolve_job(args, ctx)
    caller = _caller_user_id(ctx)
    requested = request_job_cancel(job, requested_by=caller or "owner")
    terminated = (
        terminate_recorded_task(
            job.job_dir,
            grace_seconds=code_task_limits().cancel_grace_seconds,
        )
        if requested
        else False
    )
    payload = _public_status(job)
    payload.update(
        {
            "cancel_requested": requested,
            "process_termination_requested": terminated,
        }
    )
    return ToolResult(ok=True, summary="已处理代码任务取消请求。", data=payload)


def _resume(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    job = _resolve_job(args, ctx, require_id=True)
    prompt = str(args.get("prompt") or "").strip()
    delivery_only = delivery_retry_pending(job.job_dir)
    if not prompt and not delivery_only:
        raise ValueError("prompt is required")
    request = _read_json(job.request_path)
    request_args = (
        request.get("args")
        if isinstance(request.get("args"), dict)
        else {}
    )
    title = validate_code_task_title(str(request_args.get("title") or ""))
    caller = _caller_user_id(ctx)
    attempt = append_code_task_attempt(
        job,
        prompt=prompt,
        title=title,
        delivery_only=delivery_only,
        requested_by=caller or "owner",
    )
    unit = schedule_code_task_worker(job.request_path)
    payload = _public_status(job)
    payload.update({"attempt": attempt, "worker": unit})
    return ToolResult(
        ok=True,
        summary="已提交代码任务恢复请求。",
        outputs=[str(job.job_dir)],
        data=payload,
    )


def _resolve_job(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    require_id: bool = False,
) -> BackgroundJob:
    workspace = _workspace(ctx)
    task_id = str(args.get("task_id") or "").strip()
    if require_id and not task_id:
        raise ValueError("task_id is required")
    job = find_job(workspace, task_id) if task_id else latest_code_job(
        workspace, user_id=_caller_user_id(ctx)
    )
    if job is None or job.tool_name != "start_code_task":
        raise FileNotFoundError(f"code task not found: {task_id or 'latest'}")
    caller = _caller_user_id(ctx)
    if caller and job.user_id and caller != job.user_id:
        raise PermissionError("code task belongs to another user")
    return job


def _workspace(ctx: ToolContext) -> Any:
    if ctx.workspace is not None and getattr(ctx.workspace, "root", None):
        return ctx.workspace
    if ctx.workspace_root is None:
        raise RuntimeError("workspace is unavailable")
    return SimpleNamespace(
        root=ctx.workspace_root.expanduser().resolve(),
        chat_kind=None,
        chat_id=None,
        user_id=None,
        user_name=None,
    )


def _caller_user_id(ctx: ToolContext) -> str:
    return str(getattr(ctx.workspace, "user_id", "") or "").strip()


def _public_status(job: BackgroundJob) -> dict[str, Any]:
    status = read_job_status(job) or {}
    details = status.get("details") if isinstance(status.get("details"), dict) else {}
    changed_files = details.get("changed_files")
    if not isinstance(changed_files, list):
        changes = _read_json(job.job_dir / "changes.json")
        files = changes.get("files") if isinstance(changes.get("files"), list) else []
        changed_files = [
            str(item.get("path"))
            for item in files
            if isinstance(item, dict) and item.get("path")
        ]
    validation = _read_json(job.job_dir / "validation.json")
    checks = validation.get("checks") if isinstance(validation.get("checks"), list) else []
    delivery = _read_json(job.job_dir / "delivery.json")
    state = str(status.get("status") or "unknown")
    return {
        "task_id": job.job_id,
        "status": state,
        "stage": str(status.get("stage") or state),
        "message": str(status.get("message") or ""),
        "attempt": int(status.get("attempt") or 1),
        "updated_at": status.get("updated_at"),
        "heartbeat_at": status.get("heartbeat_at"),
        "resource": (
            dict(status.get("resource"))
            if isinstance(status.get("resource"), dict)
            else {}
        ),
        "queue_position": job.queue_position,
        "changed_files": changed_files,
        "checks": checks,
        "branch": str(delivery.get("branch") or ""),
        "commit_sha": str(delivery.get("commit_sha") or ""),
        "pr_url": str(delivery.get("pr_url") or ""),
        "pr_number": (
            delivery.get("pr_number")
            if isinstance(delivery.get("pr_number"), int)
            else None
        ),
        "draft": (
            delivery.get("draft")
            if isinstance(delivery.get("draft"), bool)
            else None
        ),
        "error_code": str(status.get("error_code") or ""),
        "resumable": state in CODE_TASK_RESUMABLE_STATUSES,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


_TASK_ID = schema_property(
    type="string",
    description="Code task id returned by start_code_task. Omit only for latest-task queries.",
)
_CODE_TASK_RESULT_SCHEMA = {"type": "object", "additionalProperties": True}

TOOLS = [
    _tool(
        name="start_code_task",
        summary=(
            "Start an asynchronous isolated code-development task for this repository. "
            "Use for natural-language requests that require source, test, specification, "
            "documentation, BotSpec, adapter, dependency, or deployment changes. Return "
            "the task id immediately; do not edit source in the main conversation. When "
            "the user requested a plan before later confirmation, do not call this tool "
            "until that confirmation arrives, then submit the complete approved plan."
        ),
        input_schema=object_schema({
            "prompt": schema_property(
                type="string",
                description="Complete implementation request preserving the user's intent.",
            ),
            "title": {
                **schema_property(
                    type="string",
                    description=(
                        "Public-safe Chinese one-line title used verbatim for the draft PR."
                    ),
                ),
                "minLength": 1,
                "maxLength": 72,
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional observable acceptance criteria inferred from the request.",
                "default": [],
            },
        }, required=("prompt", "title")),
        output_schema=_CODE_TASK_RESULT_SCHEMA,
        handler=_start,
        execution_policy=EXECUTION_GLOBAL_SERIAL_BACKGROUND,
        category="development.task.write",
        weight="heavy",
    ),
    _tool(
        name="get_code_task",
        summary="Get redacted progress, heartbeat, changed files, checks, and result for a code task.",
        input_schema=object_schema({"task_id": _TASK_ID}),
        output_schema=_CODE_TASK_RESULT_SCHEMA,
        handler=_status,
        category="development.task.read",
    ),
    _tool(
        name="cancel_code_task",
        summary=(
            "Idempotently cancel a queued or running code task and terminate its execution "
            "group. Draft-PR delivery is non-cancellable once it starts."
        ),
        input_schema=object_schema({"task_id": _TASK_ID}),
        output_schema=_CODE_TASK_RESULT_SCHEMA,
        handler=_cancel,
        category="development.task.write",
    ),
    _tool(
        name="resume_code_task",
        summary=(
            "Resume a failed, cancelled, or interrupted code task. Delivery failures retry "
            "the retained commit without invoking Codex again."
        ),
        input_schema=object_schema({
            "task_id": _TASK_ID,
            "prompt": schema_property(
                type="string",
                description=(
                    "Corrective instruction for a new Codex attempt. Optional when only "
                    "retrying an already validated PR delivery."
                ),
            ),
        }, required=("task_id",)),
        output_schema=_CODE_TASK_RESULT_SCHEMA,
        handler=_resume,
        category="development.task.write",
    ),
]


__all__ = ["TOOLS"]
