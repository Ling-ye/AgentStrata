"""后台任务派发：watch 子进程结果 + 飞书通知 + job 状态查询。

ACP server 通过 ``JobDispatcher`` 把后台任务相关的协程/同步逻辑全部委托过来，
保持 server.py 只关心 ACP 协议帧调度。本模块对外暴露：

- ``extract_job_status_query``: 从 user_text 里识别 job_id（user 主动查任务进展）
- ``format_job_accepted``: 任务提交成功时给用户的回执文案
- ``JobDispatcher``: 封装 watch / send / replay 的协程，只依赖显式 host port
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from chatcopilot.contracts.code_tasks import CODE_TASK_TOOL
from chatcopilot.contracts.tools import ToolResult
from chatcopilot.core.jobs import (
    latest_code_job,
    request_job_cancel,
)
from chatcopilot.external_tools.dev.code_task_runtime import (
    code_task_limits,
    terminate_recorded_task,
)
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.runtime.jobs import (
    BackgroundJob,
    find_job,
    job_notification_workspace,
    list_unnotified_completed_jobs,
    read_job_result,
    read_job_status,
    submit_tool_job,
    write_job_notification,
)
from chatcopilot.middleware.runtime.jobs.notification import read_json_file
from chatcopilot.middleware.runtime.tasks import complete_delegated_task
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.platforms import router as _platform_router
from chatcopilot.project import ENV_PREFIX

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.job_dispatch")

# 不用 ``\b``：Python re 在 unicode 模式下把汉字也算 word char，"告诉我job_xxx" 这种
# 中文紧贴的写法在 \b 下不会被识别为 word boundary，导致 ACP 短路完全失效（用户提问
# 全文走 LLM 工具循环）。改成 lookbehind / lookahead 显式要求前后是 "非 ASCII
# word char"，覆盖中文 / 空格 / 标点 / 行首行尾。
_JOB_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(job_\d{8}_\d{6}_[0-9a-fA-F]{8})(?![A-Za-z0-9_])")
_JOB_STATUS_INTENT_RE = re.compile(
    r"完成|处理完|执行完|跑完|结束|状态|结果|成功|失败|有没有|查一下|查询|done|status|result",
    re.IGNORECASE,
)
_CODE_TASK_COMMAND_RE = re.compile(
    r"^\s*/(?P<action>task|cancel)(?:\s+(?P<job>job_\d{8}_\d{6}_[0-9a-fA-F]{8}))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class JobDispatchPort:
    """Minimal ACP host capabilities required by background job delivery."""

    connection: Callable[[], Any]
    runtime: Callable[[], Any]
    loop: Callable[[], Any]
    watch_tasks: MutableMapping[str, Any]
    make_text_update: Callable[[str], Any]


# ----------------------------------------------------------------------------
# Formatters
# ----------------------------------------------------------------------------
def format_job_accepted(job: BackgroundJob) -> str:
    if job.tool_name == CODE_TASK_TOOL:
        position = f"当前排队位置约为第 {job.queue_position} 位。" if job.queue_position else ""
        return (
            f"代码任务已进入隔离执行队列，任务 ID: {job.job_id}。{position}"
            "我会推送阶段变化和每五分钟进度摘要；可用 /task 或 /cancel 管理。"
        )
    scope = "全局迁移队列" if job.execution_policy == "global_serial_background" else "你的分析队列"
    position = f"当前排队位置约为第 {job.queue_position} 位。" if job.queue_position else ""
    return f"已加入{scope}，任务 ID: {job.job_id}。{position}我会在处理完成后在这里通知你。"


def format_job_result(job: BackgroundJob, result: Dict[str, Any]) -> str:
    ok = bool(result.get("ok"))
    elapsed = _format_elapsed(result.get("started_at"), result.get("finished_at"))
    if ok:
        lines = [
            f"后台任务已完成：{job.tool_name}",
            f"任务 ID: {job.job_id}",
        ]
        if elapsed:
            lines.append(f"耗时: {elapsed}")
        summary = str(result.get("summary") or "").strip()
        if summary:
            lines.append(summary)
        outputs = result.get("outputs") or []
        if outputs:
            lines.append(f"产物数量: {len(outputs)}")
        return "\n".join(lines)

    error = str(result.get("error") or "未知错误").strip()
    error_code = str(result.get("error_code") or "").strip()
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    failed_stage = str(details.get("failed_stage") or result.get("stage") or "").strip()
    console_tail = (
        "" if job.tool_name == CODE_TASK_TOOL else str(result.get("console_tail") or "").strip()
    )
    lines = [
        f"后台任务失败：{job.tool_name}",
        f"任务 ID: {job.job_id}",
    ]
    if elapsed:
        lines.append(f"耗时: {elapsed}")
    if failed_stage:
        lines.append(f"Failed stage: {failed_stage}")
    if error_code:
        lines.append(f"Error code: {error_code}")
    lines.append(error[:1200])
    if console_tail:
        lines.append("日志摘要：")
        lines.append(console_tail[-1200:])
    return "\n".join(lines)


def format_job_status(job: BackgroundJob, status: Optional[Dict[str, Any]]) -> str:
    status = status or {}
    state = str(status.get("status") or "unknown")
    stage = str(status.get("stage") or state)
    error_code = str(status.get("error_code") or "").strip()
    message = str(status.get("message") or "").strip()
    lines = [
        f"后台任务状态：{job.tool_name or 'unknown'}",
        f"任务 ID: {job.job_id}",
        f"状态: {state}",
    ]
    lines.append(f"Stage: {stage}")
    if error_code:
        lines.append(f"Error code: {error_code}")
    if message:
        lines.append(message)
    heartbeat = status.get("heartbeat_at")
    if heartbeat:
        try:
            age = max(0, int(time.time() - float(heartbeat)))
            lines.append(f"Heartbeat: {age}s ago")
        except (TypeError, ValueError):
            pass
    resource = status.get("resource") if isinstance(status.get("resource"), dict) else {}
    if resource:
        rss = int(resource.get("rss_bytes") or 0)
        disk = int(resource.get("disk_bytes") or 0)
        lines.append(f"Resources: rss={rss // (1024**2)}MiB disk={disk // (1024**2)}MiB")
    if job.tool_name == CODE_TASK_TOOL:
        details = status.get("details") if isinstance(status.get("details"), dict) else {}
        changed = details.get("changed_files")
        if isinstance(changed, list):
            lines.append(f"Changed files: {len(changed)}")
        return "\n".join(lines)
    progress_tail = _tail_stdout_progress(job.job_dir / "stdout.log", lines_n=10)
    if progress_tail:
        lines.append("最近进度（stdout 末尾）：")
        lines.append(progress_tail)
    return "\n".join(lines)


def _tail_stdout_progress(stdout_path: Path, *, lines_n: int = 10) -> str:
    """读取 worker stdout.log 末尾 N 行用作"任务还活着且在做什么"的证据。

    今天 case 教训：仅看 status.json 的 ``running`` + ``message`` 太薄，用户问
    "完了吗" 时机器人无法给出 "进度 N/M"、"分片上传中" 这类关键信号；只能回 "还在
    处理"，导致用户既不知道进度，也判断不出是不是卡住。把 stdout 末尾若干行附进
    短路返回里，进度 / 报错 / 子工具调用都看得到。
    """
    if not stdout_path.is_file():
        return ""
    try:
        text = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail_lines = [line for line in text.splitlines() if line.strip()][-lines_n:]
    if not tail_lines:
        return ""
    return "\n".join(f"  {line}" for line in tail_lines)


def _format_elapsed(started: Any, finished: Any) -> str:
    try:
        seconds = float(finished) - float(started)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}m{sec:02d}s"


def _resolve_job_output_files(
    ws: Workspace, result: Dict[str, Any], *, sender_mod: Any
) -> list[Path]:
    """从后台任务 outputs 中筛出当前工作区内的真实文件，忽略目录产物。

    ``sender_mod`` 由 router 按当前 BotSpec 的 platform.type 选定（飞书 /
    微信 / 后续平台共用 ``resolve_sendable_paths`` 同名 API）。
    """
    outputs = result.get("outputs") or []
    if not isinstance(outputs, list):
        return []

    files: list[Path] = []
    seen: set[str] = set()
    for raw in outputs:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            resolved = sender_mod.resolve_sendable_paths(ws, [raw])
        except (FileNotFoundError, PermissionError, ValueError):
            continue
        for path in resolved:
            if not path.is_file():
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files


def _job_poll_interval() -> float:
    try:
        return max(0.5, float(os.environ.get(f"{ENV_PREFIX}_JOB_WATCH_INTERVAL", "2")))
    except ValueError:
        return 2.0


# ----------------------------------------------------------------------------
# 文本入口
# ----------------------------------------------------------------------------
def extract_job_status_query(text: str) -> Optional[str]:
    match = _JOB_ID_RE.search(text or "")
    if match is None:
        return None
    # 只要用户显式带 job id，多数情况下都是在查任务；有状态词时更确定。
    if _JOB_STATUS_INTENT_RE.search(text or ""):
        return match.group(1)
    return match.group(1)


def extract_code_task_command(text: str) -> tuple[str, str] | None:
    match = _CODE_TASK_COMMAND_RE.fullmatch(text or "")
    if match is None:
        return None
    return match.group("action").lower(), str(match.group("job") or "")


# ----------------------------------------------------------------------------
# Dispatcher：绑定 ACP server 的连接/loop/sessions
# ----------------------------------------------------------------------------
class JobDispatcher:
    """Dispatch background-job watches and delivery through an ACP host port."""

    def __init__(self, host: JobDispatchPort) -> None:
        self._host = host

    def _platform_type(self) -> str:
        runtime = self._host.runtime()
        return getattr(runtime, "platform_type", "feishu") if runtime is not None else "feishu"

    def _sender_module(self) -> Any:
        return _platform_router.get_sender(self._platform_type())

    def _notifier_module(self) -> Any:
        return _platform_router.get_notifier(self._platform_type())

    # ------------------------------------------------------------------
    # background_submitter 工厂：注入到 AgentRuntime.new_session
    # ------------------------------------------------------------------
    def make_background_submitter(
        self,
        *,
        session_id: str,
        ws: Workspace,
    ) -> Any:
        def submitter(tool: Any, args: Dict[str, Any]) -> ToolResult:
            from chatcopilot.agent.trace import current_trace

            trace = current_trace()
            job = submit_tool_job(
                tool_name=tool.name,
                args=args,
                execution_policy=tool.execution_policy,
                workspace=ws,
                session_id=session_id,
                trace_id=trace.trace_id if trace is not None else None,
            )
            self.schedule_job_watch(job)
            return ToolResult(
                ok=True,
                summary=format_job_accepted(job),
                outputs=[str(job.job_dir)],
                console="",
                doc_links=[],
                artifact_kinds=["directory"],
            )

        return submitter

    # ------------------------------------------------------------------
    # watch loop
    # ------------------------------------------------------------------
    def schedule_job_watch(self, job: BackgroundJob) -> None:
        loop = self._host.loop()
        if loop is None or not loop.is_running():
            _LOGGER.warning(
                "background job watch not scheduled | job_id=%s session_id=%s loop_ready=%s",
                job.job_id,
                job.session_id,
                bool(loop),
            )
            return
        future = asyncio.run_coroutine_threadsafe(self._watch_background_job(job), loop)

        def _cleanup(_future: Any) -> None:
            self._host.watch_tasks.pop(job.job_id, None)

        future.add_done_callback(_cleanup)
        self._host.watch_tasks[job.job_id] = future
        _LOGGER.info(
            "background job watch scheduled | job_id=%s session_id=%s",
            job.job_id,
            job.session_id,
        )

    async def _watch_background_job(self, job: BackgroundJob) -> None:
        _LOGGER.info(
            "background job watch started | job_id=%s session_id=%s", job.job_id, job.session_id
        )
        last_stage = ""
        last_progress_sent = time.monotonic()
        while True:
            result = read_job_result(job)
            if result is not None:
                _LOGGER.info("background job result detected | job_id=%s", job.job_id)
                await self.send_job_result(job, result)
                return
            if job.tool_name == CODE_TASK_TOOL:
                status = read_job_status(job) or {}
                stage = str(status.get("stage") or status.get("status") or "")
                now = time.monotonic()
                stage_changed = bool(stage and stage not in {"queued"} and stage != last_stage)
                periodic = now - last_progress_sent >= 300
                if stage_changed or periodic:
                    await self.send_job_progress(job, status)
                    last_progress_sent = now
                if stage:
                    last_stage = stage
            await asyncio.sleep(_job_poll_interval())

    async def send_job_progress(
        self,
        job: BackgroundJob,
        status: Dict[str, Any],
    ) -> None:
        notify_ws = job_notification_workspace(job)
        notifier_mod = self._notifier_module()
        try:
            await asyncio.to_thread(
                notifier_mod.send_text_to_workspace,
                notify_ws,
                format_job_status(job, status),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("code task progress delivery failed | job_id=%s", job.job_id)

    # ------------------------------------------------------------------
    # send: result / status / replay
    # ------------------------------------------------------------------
    async def send_job_result(
        self,
        job: BackgroundJob,
        result: Dict[str, Any],
        *,
        fallback_workspace: Optional[Workspace] = None,
    ) -> None:
        notify_ws = job_notification_workspace(job, fallback=fallback_workspace)
        request = read_json_file(job.request_path) or {}
        task_id = str(request.get("task_id") or "")
        if task_id:
            try:
                complete_delegated_task(
                    notify_ws,
                    task_id=task_id,
                    job_id=job.job_id,
                    result=result,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "background job parent task merge failed | job_id=%s task_id=%s",
                    job.job_id,
                    task_id,
                )
        text = format_job_result(job, result)
        notifier_mod = self._notifier_module()
        sender_mod = self._sender_module()
        try:
            delivery = await asyncio.to_thread(
                notifier_mod.send_text_to_workspace,
                notify_ws,
                text,
            )
            output_files = (
                _resolve_job_output_files(notify_ws, result, sender_mod=sender_mod)
                if result.get("ok")
                else []
            )
            if output_files:
                await asyncio.to_thread(
                    sender_mod.send_via_cc_connect,
                    output_files,
                    f"后台任务产物：{job.tool_name}（{job.job_id}）",
                )
            write_job_notification(
                job,
                session_id=job.session_id,
                delivery="delivered",
                receive_id_type=delivery.receive_id_type,
                receive_id=delivery.receive_id,
                message_id=delivery.message_id,
            )
            _LOGGER.info(
                "background job result delivered | job_id=%s receive_id_type=%s receive_id=%s",
                job.job_id,
                delivery.receive_id_type,
                delivery.receive_id,
            )
        except Exception as exc:  # noqa: BLE001
            target = None
            try:
                target = notifier_mod.resolve_delivery_target(notify_ws)
            except Exception:  # noqa: BLE001
                target = None
            write_job_notification(
                job,
                session_id=job.session_id,
                delivery="failed",
                last_error=f"{type(exc).__name__}: {exc}",
                receive_id_type=target.receive_id_type if target else None,
                receive_id=target.receive_id if target else None,
            )
            _LOGGER.exception("background job result delivery failed | job_id=%s", job.job_id)

    async def send_job_status(self, session_id: str, session: SessionState, job_id: str) -> None:
        job = find_job(session.workspace, job_id)
        if job is None:
            text = f"没有在当前会话工作区找到后台任务：{job_id}"
        else:
            result = read_job_result(job)
            if result is not None:
                text = format_job_result(job, result)
            else:
                text = format_job_status(job, read_job_status(job))
        await self._host.connection().session_update(
            session_id=session_id,
            update=self._host.make_text_update(text),
        )

    async def handle_code_task_control(
        self,
        session_id: str,
        session: SessionState,
        action: str,
        job_id: str,
    ) -> str:
        if str(getattr(session.role, "value", session.role)).lower() != "owner":
            text = "代码任务控制仅限 Owner。"
        else:
            job = (
                find_job(session.workspace, job_id)
                if job_id
                else latest_code_job(
                    session.workspace,
                    user_id=session.workspace.user_id,
                )
            )
            if (
                job is None
                or job.tool_name != CODE_TASK_TOOL
                or (
                    job.user_id
                    and session.workspace.user_id
                    and job.user_id != session.workspace.user_id
                )
            ):
                text = f"没有找到当前 Owner 的代码任务：{job_id or 'latest'}"
            elif action == "cancel":
                requested = request_job_cancel(
                    job,
                    requested_by=session.workspace.user_id or "owner",
                )
                terminated = (
                    terminate_recorded_task(
                        job.job_dir,
                        grace_seconds=code_task_limits().cancel_grace_seconds,
                    )
                    if requested
                    else False
                )
                status = read_job_status(job) or {}
                text = format_job_status(job, status)
                text += (
                    "\n取消请求已记录。"
                    if requested
                    else "\n任务已进入交付阶段或终态，取消未被接受。"
                )
                if terminated:
                    text += "\n执行进程终止信号已发送。"
            else:
                result = read_job_result(job)
                text = (
                    format_job_result(job, result)
                    if result is not None
                    else format_job_status(job, read_job_status(job))
                )
        await self._host.connection().session_update(
            session_id=session_id,
            update=self._host.make_text_update(text),
        )
        return text

    async def send_unnotified_completed_jobs(self, session_id: str, session: SessionState) -> None:
        try:
            jobs = list_unnotified_completed_jobs(
                session.workspace,
                limit=5,
            )
            for job in jobs:
                result = read_job_result(job)
                if result is None:
                    continue
                _LOGGER.info(
                    "background job unnotified result replay | job_id=%s session_id=%s",
                    job.job_id,
                    session_id,
                )
                await self.send_job_result(job, result, fallback_workspace=session.workspace)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("background job replay scan failed | sid=%s", session_id)


__all__ = [
    "JobDispatchPort",
    "JobDispatcher",
    "extract_job_status_query",
    "extract_code_task_command",
    "format_job_accepted",
    "format_job_result",
    "format_job_status",
]
