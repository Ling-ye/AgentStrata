"""后台任务子系统：提交、worker 子进程、FIFO 队列、通知元数据。

后台任务用于把长耗时工具（数据迁移、内存 diff 等）放到独立子进程，避免阻塞
ACP 主循环。submitter 启动 worker，worker 执行工具后写 result.json + status.json，
ACP 端的 job_dispatch 通过 watch 轮询结果并把通知投递到飞书。
"""
from chatcopilot.middleware.runtime.jobs.notification import (
    read_job_notification,
    write_job_notification,
)
from chatcopilot.middleware.runtime.jobs.queue import FileQueueSlot
from chatcopilot.middleware.runtime.jobs.submitter import (
    BackgroundJob,
    find_job,
    is_job_completed,
    job_notification_workspace,
    list_unnotified_completed_jobs,
    read_job_result,
    read_job_status,
    submit_tool_job,
)
from chatcopilot.middleware.runtime.jobs.worker import run_worker

__all__ = [
    "BackgroundJob",
    "FileQueueSlot",
    "find_job",
    "is_job_completed",
    "job_notification_workspace",
    "list_unnotified_completed_jobs",
    "read_job_notification",
    "read_job_result",
    "read_job_status",
    "run_worker",
    "submit_tool_job",
    "write_job_notification",
]
