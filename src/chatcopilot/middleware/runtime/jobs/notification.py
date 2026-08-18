"""后台任务通知状态的读写：负责 ACP 端把投递成功/失败的元数据持久化。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from chatcopilot.core.jobs import (
    read_json_file as _read_core_json_file,
    write_json_atomic as _write_core_json_atomic,
)


NOTIFICATION_FILENAME = "notification.json"


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    _write_core_json_atomic(path, payload)


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    return _read_core_json_file(path)


def notification_state_payload(
    *,
    job_id: str,
    session_id: Optional[str],
    delivery: str,
    attempts: int,
    channel: str,
    last_error: str = "",
    updated_at: Optional[float] = None,
    delivered_at: Optional[object] = None,
    receive_id_type: Optional[str] = None,
    receive_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "session_id": session_id,
        "delivery": delivery,
        "channel": channel,
        "attempts": attempts,
        "last_error": last_error,
        "updated_at": time.time() if updated_at is None else updated_at,
        "delivered_at": delivered_at,
        "receive_id_type": receive_id_type,
        "receive_id": receive_id,
        "message_id": message_id,
    }


def read_job_notification(job: Any) -> Optional[Dict[str, Any]]:
    """读取 job 的当前通知状态，无文件返回 None。"""
    return read_json_file(job.job_dir / NOTIFICATION_FILENAME)


def write_job_notification(
    job: Any,
    *,
    delivery: str,
    session_id: Optional[str] = None,
    channel: str = "feishu_openapi",
    last_error: str = "",
    receive_id_type: Optional[str] = None,
    receive_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """更新 job 的通知投递状态。"""
    current = read_job_notification(job) or {}
    attempts = int(current.get("attempts") or 0)
    if delivery in {"delivered", "failed"}:
        attempts += 1
    now = time.time()
    write_json_atomic(
        job.job_dir / NOTIFICATION_FILENAME,
        notification_state_payload(
            job_id=job.job_id,
            session_id=session_id,
            delivery=delivery,
            channel=channel,
            attempts=attempts,
            last_error=last_error,
            updated_at=now,
            delivered_at=now if delivery == "delivered" else current.get("delivered_at"),
            receive_id_type=receive_id_type,
            receive_id=receive_id,
            message_id=message_id,
        ),
    )


__all__ = [
    "NOTIFICATION_FILENAME",
    "notification_state_payload",
    "read_job_notification",
    "read_json_file",
    "write_job_notification",
    "write_json_atomic",
]
