"""轻量任务管理器：把长操作（sync / rebuild / dump）跑在后台线程，逐行收集日志。

- 按 instance_id 串行：同一实例同一时刻只允许一个长任务，避免「同步中又重建」互踩。
  不同实例互不影响（各自独立任务），满足多机器人并行运维。
- 每个任务的输出可被多个 SSE 订阅者实时跟读。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional


@dataclass
class Task:
    id: str
    instance_id: str
    kind: str
    status: str = "running"  # running | done | failed
    exit_code: Optional[int] = None
    lines: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def append(self, line: str) -> None:
        with self._cond:
            self.lines.append(line)
            self._cond.notify_all()

    def finish(self, status: str, exit_code: Optional[int]) -> None:
        with self._cond:
            self.status = status
            self.exit_code = exit_code
            self.finished_at = time.time()
            self._cond.notify_all()

    def follow(self, timeout: float = 30.0) -> Iterator[str]:
        """从头吐出已有行，再实时跟读新行，直到任务结束。"""
        idx = 0
        while True:
            with self._cond:
                while idx >= len(self.lines) and self.status == "running":
                    if not self._cond.wait(timeout=timeout):
                        break
                pending = self.lines[idx:]
                idx = len(self.lines)
                done = self.status != "running"
            for ln in pending:
                yield ln
            if done and idx >= len(self.lines):
                return

    def to_dict(self, *, tail: Optional[int] = None) -> Dict[str, object]:
        lines = self.lines if tail is None else self.lines[-tail:]
        return {
            "id": self.id,
            "instance_id": self.instance_id,
            "kind": self.kind,
            "status": self.status,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "lines": lines,
            "line_count": len(self.lines),
        }


class TaskManager:
    def __init__(self, *, keep: int = 50) -> None:
        self._tasks: Dict[str, Task] = {}
        self._order: List[str] = []
        self._active: Dict[str, str] = {}  # instance_id -> task_id（运行中）
        self._lock = threading.Lock()
        self._keep = keep

    def active_for(self, instance_id: str) -> Optional[Task]:
        with self._lock:
            tid = self._active.get(instance_id)
            return self._tasks.get(tid) if tid else None

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            return [self._tasks[t].to_dict(tail=1) for t in reversed(self._order)]

    def start(
        self,
        instance_id: str,
        kind: str,
        gen_factory: Callable[[], Iterator[str]],
    ) -> Task:
        with self._lock:
            existing = self._active.get(instance_id)
            if existing and self._tasks[existing].status == "running":
                raise RuntimeError(f"实例 {instance_id} 已有运行中的任务（{self._tasks[existing].kind}）")
            task = Task(id=uuid.uuid4().hex[:12], instance_id=instance_id, kind=kind)
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._active[instance_id] = task.id
            self._gc_locked()

        def _run() -> None:
            exit_code: Optional[int] = None
            try:
                for line in gen_factory():
                    if line.startswith("__EXIT__"):
                        try:
                            exit_code = int(line.split()[1])
                        except (IndexError, ValueError):
                            exit_code = 0
                        continue
                    task.append(line)
            except Exception as exc:  # noqa: BLE001 - 任务边界，错误进日志而非崩线程
                task.append(f"[ERR] 任务异常：{exc}")
                exit_code = 1
            finally:
                status = "done" if (exit_code or 0) == 0 else "failed"
                task.finish(status, exit_code)
                with self._lock:
                    if self._active.get(instance_id) == task.id:
                        self._active.pop(instance_id, None)

        threading.Thread(target=_run, name=f"task-{task.id}", daemon=True).start()
        return task

    def _gc_locked(self) -> None:
        while len(self._order) > self._keep:
            old = self._order.pop(0)
            t = self._tasks.get(old)
            if t and t.status == "running":
                self._order.insert(0, old)
                break
            self._tasks.pop(old, None)
