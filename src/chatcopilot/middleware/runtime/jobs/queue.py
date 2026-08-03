"""文件名排序的跨进程 FIFO 队列槽。"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from chatcopilot.core.jobs import queue_position, queue_root, safe_segment
from chatcopilot.project import ENV_PREFIX


def poll_interval() -> float:
    try:
        return max(0.1, float(os.environ.get(f"{ENV_PREFIX}_JOB_POLL_INTERVAL", "0.5")))
    except ValueError:
        return 0.5


class FileQueueSlot:
    """按文件名排序的跨进程 FIFO 队列槽。"""

    def __init__(self, queue_name: str, job_id: str, capacity: int = 1) -> None:
        self.queue_name = safe_segment(queue_name)
        self.job_id = safe_segment(job_id)
        self.capacity = max(1, int(capacity))
        self.root = queue_root() / self.queue_name
        self.entry: Optional[Path] = None

    def __enter__(self) -> "FileQueueSlot":
        self.root.mkdir(parents=True, exist_ok=True)
        self.entry = self.root / f"{time.time_ns()}-{os.getpid()}-{self.job_id}.queue"
        self.entry.write_text(
            f"pid={os.getpid()}\njob_id={self.job_id}\ncreated={time.time()}\n",
            encoding="utf-8",
        )
        while True:
            entries = sorted(self.root.glob("*.queue"), key=lambda p: p.name)
            if self.entry in entries[: self.capacity]:
                return self
            time.sleep(poll_interval())

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self.entry is not None:
            try:
                self.entry.unlink()
            except OSError:
                pass


__all__ = ["FileQueueSlot", "poll_interval", "queue_position", "queue_root", "safe_segment"]
