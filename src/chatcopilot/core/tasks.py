"""Platform-neutral turn task runtime facade."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from chatcopilot.core.workspace_runtime import Workspace

EVENTS_FILENAME = "events.jsonl"
TASK_FILENAME = "task.json"
TASKS_DIRNAME = "tasks"
TURN_FILENAME = "turn.json"

TASK_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(task_\d{8}_\d{6}_[0-9a-fA-F]{8})(?![A-Za-z0-9_])")
_BUDGET_STOP_REASONS = {"tool_call_cap", "timeout", "hard_timeout", "iteration_cap"}


def is_task_id(value: str) -> bool:
    return bool(TASK_ID_RE.fullmatch(str(value or "").strip()))


def find_task_dir(workspace: Workspace, task_id: str) -> Path | None:
    task_id = str(task_id or "").strip()
    if not is_task_id(task_id):
        return None
    task_dir = workspace.tasks / task_id
    if not task_dir.is_dir():
        return None
    if not (task_dir / TASK_FILENAME).is_file():
        return None
    return task_dir


def format_task_status(workspace: Workspace, task_id: str) -> tuple[str, list[str], None]:
    task_id = str(task_id or "").strip()
    if not is_task_id(task_id):
        return (
            f"不是有效的单轮任务 ID: {task_id}\n"
            "格式必须为 task_<YYYYMMDD>_<HHMMSS>_<8 位 hex>。"
            "后台任务请使用 get_job_status(job_id=...)。",
            [],
            None,
        )

    task_dir = find_task_dir(workspace, task_id)
    if task_dir is None:
        return (
            f"当前工作区内找不到单轮任务: {task_id}\n"
            f"workspace={workspace.root}\n"
            "提示：task ID 属于发起它的会话工作区；也可能已被清理。",
            [],
            None,
        )

    task = _read_json(task_dir / TASK_FILENAME)
    turn = _read_json(task_dir / TURN_FILENAME)
    event_stats = _read_event_stats(task_dir / EVENTS_FILENAME)
    tools = task.get("tools") if isinstance(task.get("tools"), list) else []
    llm_calls = task.get("llm_calls") if isinstance(task.get("llm_calls"), list) else []
    job_ids = task.get("job_ids") if isinstance(task.get("job_ids"), list) else []
    job_results = task.get("job_results") if isinstance(task.get("job_results"), list) else []
    usage_totals = task.get("usage_totals") if isinstance(task.get("usage_totals"), dict) else {}
    stop_reason = str(turn.get("stop_reason") or "").strip()
    final_text = str(turn.get("final_text") or "").strip()
    status = str(task.get("status") or "unknown")
    progress = str(task.get("progress") or "").strip()

    failed_tools = [
        str(item.get("name") or "unknown")
        for item in tools
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    tool_names = [
        str(item.get("name") or "unknown")
        for item in tools
        if isinstance(item, dict) and item.get("name")
    ]
    signals = _failure_signals(status, stop_reason, failed_tools, final_text)

    lines = [
        f"任务 ID: {task_id}",
        f"状态: {status}",
    ]
    if progress:
        lines.append(f"进展: {progress}")
    if stop_reason:
        lines.append(f"Stop reason: {stop_reason}")
    if signals:
        lines.append("失败/受限信号: " + "；".join(signals))

    lines.extend(
        [
            f"工具调用: {len(tools) or event_stats['tool_finished']} 次",
            f"LLM 调用: {len(llm_calls) or event_stats['llm_calls']} 次",
        ]
    )
    if usage_totals:
        total_tokens = usage_totals.get("total_tokens")
        if isinstance(total_tokens, (int, float)):
            lines.append(f"Token 总量: {int(total_tokens)}")
    if tool_names:
        lines.append("工具序列: " + ", ".join(tool_names[-12:]))
    if failed_tools:
        lines.append("失败工具: " + ", ".join(failed_tools))
    if job_ids:
        lines.append("关联后台任务: " + ", ".join(str(item) for item in job_ids))
    if job_ids:
        job_states = [
            _read_json(workspace.root / "jobs" / str(job_id) / "status.json")
            for job_id in job_ids
        ]
        current_state = next(
            (
                item
                for item in reversed(job_states)
                if item.get("status") not in {"succeeded", "failed"}
            ),
            job_states[-1] if job_states else {},
        )
        stage = str(current_state.get("stage") or current_state.get("status") or "").strip()
        if stage:
            lines.append(f"Current child job stage: {stage}")
        error_code = str(current_state.get("error_code") or "").strip()
        if error_code:
            lines.append(f"Child job error code: {error_code}")
    if job_results:
        failed_jobs = [
            item
            for item in job_results
            if isinstance(item, dict) and not item.get("ok")
        ]
        if failed_jobs:
            lines.append(
                "Failed child jobs: "
                + ", ".join(
                    f"{item.get('job_id')}[{item.get('error_code') or 'failed'}]"
                    for item in failed_jobs
                )
            )
    if final_text:
        lines.append("最终回复: " + _truncate(final_text, 800))

    user_text = str(turn.get("user_text") or task.get("description") or "").strip()
    if user_text:
        lines.append("用户输入: " + _truncate(user_text, 300))
    lines.append(f"任务目录: {workspace.relpath(task_dir)}")
    return ("\n".join(lines), [str(task_dir)], None)


def _failure_signals(
    status: str,
    stop_reason: str,
    failed_tools: list[str],
    final_text: str,
) -> list[str]:
    signals: list[str] = []
    if status == "failed":
        signals.append("task status=failed")
    if stop_reason in _BUDGET_STOP_REASONS:
        signals.append(f"预算/上限停止: {stop_reason}")
    if failed_tools:
        signals.append(f"{len(failed_tools)} 个工具失败")
    if "工具调用上限" in final_text or "tool_call_cap" in final_text:
        signals.append("最终回复提到工具调用上限")
    return signals


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"_value": value}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        return {"_read_error": f"{type(exc).__name__}: {exc}"}


def _read_event_stats(path: Path) -> dict[str, int]:
    stats = {"events": 0, "tool_finished": 0, "llm_calls": 0}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return stats
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stats["events"] += 1
        if event.get("event") == "tool_finished":
            stats["tool_finished"] += 1
        elif event.get("event") == "llm_call_finished":
            stats["llm_calls"] += 1
    return stats


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "TASK_ID_RE",
    "find_task_dir",
    "format_task_status",
    "is_task_id",
]
