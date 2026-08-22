"""Workspace task/job diagnostic handlers."""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.agent.tools.workspace_context import resolve_workspace
from chatcopilot.contracts.tools import HandlerResult
from chatcopilot.agent.tools.builtin.workspace.common import _format_mtime, _require, _silent_cleanup

def _handler_get_job_status(args: Dict[str, Any]) -> HandlerResult:
    """查询后台任务（jobs/<job_id>/）的实时状态 + 末尾进度。

    背景：``long_running_export`` / ``long_running_analysis`` 等长耗时
    工具会异步落到 ``jobs/<job_id>/``，worker 子进程把进度写到 ``stdout.log``，
    把当前状态写到 ``status.json``。用户问 "job_xxx 完了吗？" 时 ACP 主流程会
    通过 ``extract_job_status_query`` 文本短路直接处理；但当 LLM 在工具循环里
    需要主动查 job 时，没有专用工具就只能拼 ``list_workspace``+``read_text_head``，
    后者拿到目录路径会 FileNotFoundError，导致 LLM 误判"任务还在队列里"。
    本工具复用 ``runtime.jobs.find_job``，给 LLM 一条直达的"权威状态"路径。
    """
    from chatcopilot.core.jobs import (
        find_job,
        read_job_result,
        read_job_status,
    )

    job_id = _require(args, "job_id").strip()
    if job_id.startswith("task_"):
        return (
            f"{job_id} 是单轮对话任务 ID，不是后台 job ID。\n"
            "请改用 get_task_status(task_id=...) 查询 stop reason、最终回复、工具调用和关联 job。",
            [],
            None,
        )
    tail_lines = int(args.get("tail_lines") or 20)
    if tail_lines < 0 or tail_lines > 200:
        raise ValueError("tail_lines 必须在 [0, 200] 区间内")

    ws = resolve_workspace(create=True)
    job = find_job(ws, job_id)
    if job is None:
        return (
            f"当前工作区内找不到后台任务: {job_id}\n"
            f"workspace={ws.root}\n"
            f"提示：任务 ID 格式必须为 job_<YYYYMMDD>_<HHMMSS>_<8 位 hex>；"
            f"也可能任务属于别的会话或已被清理。",
            [],
            None,
        )

    status = read_job_status(job) or {}
    result = read_job_result(job)

    lines: List[str] = [
        f"任务 ID: {job.job_id}",
        f"工具: {job.tool_name or 'unknown'}",
        f"执行策略: {job.execution_policy or 'unknown'}",
        f"状态: {status.get('status') or 'unknown'}",
    ]
    msg = str(status.get("message") or "").strip()
    if msg:
        lines.append(f"消息: {msg}")
    if isinstance(status.get("updated_at"), (int, float)):
        lines.append(f"状态更新时间: {_format_mtime(status['updated_at'])}")
    if job.queue_position is not None:
        lines.append(f"队列位置: 第 {job.queue_position} 位")

    stdout_path = job.job_dir / "stdout.log"
    if tail_lines > 0 and stdout_path.is_file():
        try:
            text = stdout_path.read_text(encoding="utf-8", errors="replace")
            tail = [line for line in text.splitlines() if line.strip()][-tail_lines:]
            if tail:
                lines.append(f"最近进度（stdout 末尾 {len(tail)} 行）：")
                lines.extend("  " + line for line in tail)
        except OSError:
            pass

    stderr_path = job.job_dir / "stderr.log"
    if stderr_path.is_file():
        try:
            stderr_size = stderr_path.stat().st_size
            if stderr_size > 0:
                err_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                err_tail = [line for line in err_text.splitlines() if line.strip()][-5:]
                if err_tail:
                    lines.append("stderr 末尾 5 行（任务异常时优先看这里）：")
                    lines.extend("  " + line for line in err_tail)
        except OSError:
            pass

    if result is not None:
        outputs = result.get("outputs") or []
        ok = bool(result.get("ok"))
        lines.append(f"任务已完成（ok={ok}），产物数量: {len(outputs)}")
        summary = str(result.get("summary") or "").strip()
        if summary:
            lines.append(f"完成摘要: {summary[:500]}")
        err = str(result.get("error") or "").strip()
        if err and not ok:
            lines.append(f"失败原因: {err[:500]}")

    return ("\n".join(lines), [str(job.job_dir)], None)


def _handler_get_task_status(args: Dict[str, Any]) -> HandlerResult:
    from chatcopilot.core.tasks import format_task_status

    task_id = _require(args, "task_id").strip()
    ws = resolve_workspace(create=True)
    try:
        return format_task_status(ws, task_id)
    finally:
        _silent_cleanup(ws)


__all__ = ["_handler_get_job_status", "_handler_get_task_status"]
