"""后台任务 worker 入口：``python -m chatcopilot.middleware.runtime.jobs.worker``。"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict

from chatcopilot.contracts.code_tasks import CODE_TASK_TERMINAL_STATUSES, CODE_TASK_TOOL
from chatcopilot.core.jobs import (
    BackgroundJob,
    read_job_result,
    read_json_file as read_core_json,
)
from chatcopilot.middleware.runtime.jobs.notification import write_json_atomic
from chatcopilot.middleware.runtime.jobs.queue import FileQueueSlot
from chatcopilot.middleware.runtime.jobs.submitter import (
    queue_capacity,
    workspace_env,
    write_status,
)
from chatcopilot.project import ENV_PREFIX

_RESULT_FILENAME = "result.json"


def run_worker(request_path: Path) -> int:
    """worker 入口：进入队列，执行一个工具，写出 result.json。"""
    job_dir = request_path.parent
    if (
        request_path.name != "request.json"
        or job_dir.parent.name != "jobs"
        or not job_dir.name.startswith("job_")
    ):
        return 2
    request = read_core_json(request_path)
    if (
        not isinstance(request, dict)
        or str(request.get("job_id") or "") != job_dir.name
        or not str(request.get("tool_name") or "").strip()
    ):
        return 2
    job_id = str(request.get("job_id") or job_dir.name)
    queue_name = str(request.get("queue_name") or "default")
    policy = str(request.get("execution_policy") or "")
    tool_name = str(request.get("tool_name") or "")
    started = time.time()

    ws_env = workspace_env(request.get("workspace") or {})
    os.environ.update(ws_env)
    os.environ[f"{ENV_PREFIX}_BACKGROUND_WORKER"] = "1"

    from chatcopilot.core.log_context import bind_log_context
    from chatcopilot.core.logging import configure_logging

    configure_logging()

    agent_runtime = None
    current_stage = "queued"
    try:
        context = bind_log_context(
            task_id=request.get("task_id"),
            trace_id=request.get("trace_id"),
            session_id=(request.get("notify") or {}).get("session_id"),
            job_id=job_id,
        )
        context.__enter__()
        with FileQueueSlot(queue_name, job_id, queue_capacity(policy)):
            current_stage = "preparing"
            write_status(
                job_dir,
                "preparing" if tool_name == CODE_TASK_TOOL else "running",
                "Job is preparing.",
                stage=current_stage,
            )
            # local import 避免 worker 启动期 import agent runtime 的额外开销
            from chatcopilot.contracts.jobs import JobExecutionContext
            from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService

            workspace_service = MiddlewareWorkspaceService()
            def update_job_stage(
                stage: str,
                message: str,
                error_code: str = "",
                details: Any = None,
            ) -> None:
                nonlocal current_stage
                current_stage = stage
                write_status(
                    job_dir,
                    stage if tool_name == CODE_TASK_TOOL else "running",
                    message,
                    stage=stage,
                    error_code=error_code,
                    details=dict(details or {}),
                )

            job_context = JobExecutionContext(
                job_id=job_id,
                job_dir=job_dir,
                update_status=update_job_stage,
            )
            executor, agent_runtime = _build_background_executor(
                tool_name=tool_name,
                job_id=job_id,
                workspace_service=workspace_service,
                job_context=job_context,
            )
            result = executor.execute(tool_name, request.get("args") or {})
            failure_stage = result.stage or current_stage or "executing"
            persisted = read_core_json(job_dir / "status.json") or {}
            persisted_state = str(persisted.get("status") or "")
            final_stage = (
                "succeeded"
                if result.ok
                else (
                    persisted_state
                    if tool_name == CODE_TASK_TOOL
                    and persisted_state in CODE_TASK_TERMINAL_STATUSES
                    else "failed"
                )
            )
            result_details = dict(result.details or {})
            if not result.ok:
                result_details.setdefault("failed_stage", failure_stage)
            payload: Dict[str, Any] = {
                "job_id": job_id,
                "tool_name": request.get("tool_name"),
                "ok": result.ok,
                "summary": result.summary,
                "outputs": result.outputs,
                "error": result.error,
                "error_code": result.error_code,
                "details": result_details,
                "stage": final_stage,
                "console_tail": (result.console or "")[-4000:],
                "started_at": started,
                "finished_at": time.time(),
            }
            write_json_atomic(job_dir / _RESULT_FILENAME, payload)
            persisted_result = read_job_result(
                BackgroundJob(
                    job_id=job_id,
                    tool_name=tool_name,
                    execution_policy=policy,
                    job_dir=job_dir,
                    request_path=request_path,
                    result_path=job_dir / _RESULT_FILENAME,
                )
            )
            if not isinstance(persisted_result, dict):
                raise OSError("persisted job result could not be read")
            persisted_ok = bool(persisted_result.get("ok"))
            persisted_stage = str(
                persisted_result.get("stage")
                or ("succeeded" if persisted_ok else "failed")
            )
            persisted_error_code = str(persisted_result.get("error_code") or "")
            persisted_error = str(persisted_result.get("error") or "")
            persisted_summary = str(persisted_result.get("summary") or "")
            persisted_details = (
                dict(persisted_result.get("details") or {})
                if isinstance(persisted_result.get("details"), dict)
                else {}
            )
            write_status(
                job_dir,
                persisted_stage,
                persisted_summary
                if persisted_ok
                else (persisted_error or "Tool execution failed."),
                stage=persisted_stage,
                error_code=persisted_error_code,
                details=persisted_details,
            )
            _finish_attempt(
                request_path,
                attempt=int((read_core_json(job_dir / "status.json") or {}).get("attempt") or 1),
                status=persisted_stage,
                error_code=persisted_error_code,
                error=persisted_error,
            )
            return 0 if persisted_ok else 1
    except Exception as exc:  # noqa: BLE001
        payload = {
            "job_id": job_id,
            "tool_name": request.get("tool_name"),
            "ok": False,
            "summary": "",
            "outputs": [],
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "worker_error",
            "details": {},
            "stage": current_stage or "failed",
            "traceback": traceback.format_exc(),
            "console_tail": "",
            "started_at": started,
            "finished_at": time.time(),
        }
        write_json_atomic(job_dir / _RESULT_FILENAME, payload)
        write_status(
            job_dir,
            "failed",
            payload["error"],
            stage=payload["stage"],
            error_code=payload["error_code"],
        )
        _finish_attempt(
            request_path,
            attempt=int((read_core_json(job_dir / "status.json") or {}).get("attempt") or 1),
            status="failed",
            error_code=payload["error_code"],
            error=payload["error"],
        )
        return 1
    finally:
        if agent_runtime is not None:
            agent_runtime.close()
        if "context" in locals():
            context.__exit__(None, None, None)


def _build_background_executor(
    *,
    tool_name: str,
    job_id: str,
    workspace_service: Any,
    job_context: Any = None,
):
    from chatcopilot.agent.tools.executor import ToolExecutor

    if tool_name != 'run_coding_workflow':
        return ToolExecutor(
            workspace_service=workspace_service,
            job_context=job_context,
        ), None

    from chatcopilot.core.config import load_config
    from chatcopilot.agent.runtime import build_agent_runtime
    from chatcopilot.agent.context.prompt_plan import PromptBuildInput
    from chatcopilot.botspec.runtime import load_runtime_context
    from chatcopilot.botspec.runtime_env import apply_runtime_env, load_research_llm_config

    runtime_context = load_runtime_context()
    apply_runtime_env(runtime_context)
    chat_config = load_config(env_prefix=runtime_context.spec.llm.env_prefix)
    agent_runtime = build_agent_runtime(
        chat_config=chat_config,
        research_llm_config=load_research_llm_config(
            runtime_context.spec.llm,
            fallback=chat_config.llm,
        ),
        tool_packs=tuple(
            pack for pack in runtime_context.tool_packs if pack != "persona.control"
        ),
        exclude_tools=runtime_context.exclude_tools,
        skill_index=runtime_context.skills,
        rag_sources=runtime_context.rag_sources,
        mcp_servers=runtime_context.mcp_servers,
        subagents=runtime_context.subagents,
        agent_backend=getattr(runtime_context, "agent_backend", "native"),
    )
    session = agent_runtime.new_session(
        session_id=f"background-{job_id}",
        prompt_input=PromptBuildInput(
            profile=runtime_context.prompt_profile,
            backend=runtime_context.agent_backend,
            model=None,
            role="owner",
            channel_kind="private",
            session_policy="这是受保护的后台工具任务会话；只执行已持久化的当前任务。",
            capability_policies=runtime_context.capability_policies,
            skill_index=runtime_context.skills,
        ),
        workspace_service=workspace_service,
        caller_role_hint="owner",
    )
    return session.tool_executor, agent_runtime


def _finish_attempt(
    request_path: Path,
    *,
    attempt: int,
    status: str,
    error_code: str,
    error: str,
) -> None:
    request = read_core_json(request_path) or {}
    attempts = request.get("attempts")
    if not isinstance(attempts, list):
        return
    now = time.time()
    for item in attempts:
        if not isinstance(item, dict) or int(item.get("number") or 0) != attempt:
            continue
        item.update(
            {
                "status": status,
                "finished_at": now,
                "error_code": error_code,
                "error": error[-2000:],
            }
        )
        session = read_core_json(request_path.parent / "codex-session.json") or {}
        if session.get("native_session_id"):
            item["native_session_id"] = session["native_session_id"]
        break
    write_json_atomic(request_path, request)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: python -m chatcopilot.middleware.runtime.jobs.worker <request.json>",
            file=sys.stderr,
        )
        return 2
    return run_worker(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_worker"]
