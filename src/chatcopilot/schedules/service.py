"""Public management and execution state transitions for scheduled bot work."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
import uuid

from chatcopilot.schedules.models import ACTIVE_STATES, ScheduleError, ScheduleSettings, occurrence, research_input
from chatcopilot.schedules.repository import ScheduleRepository


class ScheduleService:
    def __init__(self, root: Path, *, clock=time.time):
        self.store = ScheduleRepository(root)
        self.clock = clock

    def overview(self) -> dict:
        with self.store.transaction() as tx:
            host = tx.host()
            available = bool(host and host["state"] == "running" and 0 <= self.clock() - host["heartbeat_at"] < 30)
            tasks = []
            for task in tx.tasks():
                recent = tx.runs(task["id"], limit=1)
                active = tx.runs(task["id"], active=True, limit=1)
                tasks.append({**task, "last_run": _summary(recent[0]) if recent else None,
                              "active_run": _summary(active[0]) if active else None})
            return {"tasks": tasks, "host": {**(host or {}), "available": available}}

    def create(self, settings: ScheduleSettings, *, request_id: str) -> dict:
        task_id = "schedule_" + hashlib.sha256(_request_id(request_id).encode()).hexdigest()[:24]
        now = self.clock()
        with self.store.transaction(write=True) as tx:
            existing = tx.task(task_id)
            if existing:
                if existing["settings"] != settings.model_dump():
                    raise ScheduleError("conflict", "创建请求已被用于另一份配置")
                return existing
            task = {"id": task_id, "revision": 1, "settings": settings.model_dump(),
                    "next_run": occurrence(settings, now, forward=True) if settings.enabled else None,
                    "created_at": now, "updated_at": now}
            tx.save_task(task)
            return task

    def update(self, task_id: str, settings: ScheduleSettings, *, revision: int) -> dict:
        with self.store.transaction(write=True) as tx:
            task = _task(tx, task_id, revision)
            now = self.clock()
            task = {**task, "settings": settings.model_dump(), "revision": revision + 1,
                    "updated_at": now, "next_run": occurrence(settings, now, forward=True) if settings.enabled else None}
            tx.save_task(task)
            # The running snapshot stays immutable; queued work cannot outlive its revision.
            for run in tx.runs(task_id, active=True):
                run["cancel_requested"] = True
                if run["status"] == "queued":
                    run.update(status="cancelled", finished_at=now, error_code="configuration_changed")
                tx.save_run(run)
            return task

    def delete(self, task_id: str, *, revision: int) -> None:
        with self.store.transaction(write=True) as tx:
            _task(tx, task_id, revision)
            if tx.runs(task_id, active=True, limit=1):
                raise ScheduleError("conflict", "任务还有运行中的记录，请先停止并等待结束")
            tx.delete_task(task_id)

    def history(self, *, task_id: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise ScheduleError("invalid_request", "分页参数无效")
        with self.store.transaction() as tx:
            rows = tx.runs(task_id, limit=limit + 1, offset=offset)
            return {"runs": [_summary(row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def detail(self, run_id: str) -> dict:
        with self.store.transaction() as tx:
            return _run(tx, run_id)

    def request_run(self, task_id: str, *, revision: int, preview: bool, request_id: str) -> dict:
        key = "manual:" + _request_id(request_id)
        with self.store.transaction(write=True) as tx:
            previous = tx.requested_run(key)
            if previous:
                if (previous["task_id"], previous["revision"], previous["preview"]) != (task_id, revision, preview):
                    raise ScheduleError("conflict", "执行请求已被使用")
                return previous
            task = _task(tx, task_id, revision)
            if tx.runs(task_id, active=True, limit=1):
                raise ScheduleError("conflict", "同一个任务已有待执行或运行中的记录")
            run = _new_run(task, self.clock(), self.clock(), key, preview=preview, trigger="manual")
            tx.save_run(run)
            return run

    def cancel(self, run_id: str) -> dict:
        with self.store.transaction(write=True) as tx:
            run = _run(tx, run_id)
            if run["status"] in ACTIVE_STATES:
                run["cancel_requested"] = True
                if run["status"] == "queued":
                    run.update(status="cancelled", finished_at=self.clock(), error_code="cancelled")
                tx.save_run(run)
            return run

    def enqueue_due(self) -> None:
        now = self.clock()
        with self.store.transaction(write=True) as tx:
            for task in tx.tasks():
                if task["next_run"] is None or task["next_run"] > now:
                    continue
                settings = ScheduleSettings.model_validate(task["settings"])
                scheduled_for = occurrence(settings, now, forward=False)
                task["next_run"] = occurrence(settings, now, forward=True)
                tx.save_task(task)
                key = f"timer:{task['id']}:{task['revision']}:{scheduled_for}"
                if tx.requested_run(key):
                    continue
                run = _new_run(task, scheduled_for, now, key, preview=False, trigger="timer")
                active = tx.runs(task["id"], active=True)
                if any(previous["status"] != "queued" or previous["trigger"] != "timer" for previous in active):
                    run.update(status="skipped", finished_at=now, error_code="previous_run_active")
                else:
                    for previous in active:
                        previous.update(status="skipped", finished_at=now, error_code="superseded_schedule")
                        tx.save_run(previous)
                tx.save_run(run)

    def claim(self) -> dict | None:
        with self.store.transaction(write=True) as tx:
            active = tx.runs(active=True)
            if any(run["status"] != "queued" for run in active):
                return None
            for run in reversed(active):
                task = tx.task(run["task_id"])
                if not task or task["revision"] != run["revision"] or run["cancel_requested"]:
                    run.update(status="cancelled", finished_at=self.clock(), error_code="configuration_changed")
                    tx.save_run(run)
                    continue
                run.update(status="researching", started_at=self.clock())
                tx.save_run(run)
                return run
            return None

    def requeue(self, run_id: str) -> None:
        with self.store.transaction(write=True) as tx:
            run = _run(tx, run_id)
            run.update(status="cancelled" if run["cancel_requested"] else "queued", started_at=None)
            if run["cancel_requested"]:
                run.update(finished_at=self.clock(), error_code="cancelled")
            tx.save_run(run)

    def generated(self, run_id: str, text: str) -> bool:
        with self.store.transaction(write=True) as tx:
            run = _run(tx, run_id)
            task = tx.task(run["task_id"])
            allowed = bool(task and task["revision"] == run["revision"] and not run["cancel_requested"])
            run.update(result_text=text, generated_at=self.clock())
            if not allowed:
                run.update(status="cancelled", finished_at=self.clock(), error_code="configuration_changed")
            elif not run["preview"]:
                # Persist intent before the first external submission; recovery never resends.
                run.update(status="delivering", delivery_started_at=self.clock())
            tx.save_run(run)
            return allowed

    def finish(self, run_id: str, outcome: dict) -> None:
        with self.store.transaction(write=True) as tx:
            run = _run(tx, run_id)
            run.update(outcome, finished_at=self.clock())
            tx.save_run(run)

    def unfinished(self) -> list[dict]:
        with self.store.transaction() as tx:
            return tx.runs(active=True)

    def heartbeat(self, state: str, error_code: str = "") -> None:
        with self.store.transaction(write=True) as tx:
            tx.save_host({"state": state, "heartbeat_at": self.clock(), "error_code": error_code})


def _task(tx, task_id: str, revision: int) -> dict:
    task = tx.task(task_id)
    if task is None:
        raise ScheduleError("not_found", "定时任务不存在")
    if task["revision"] != revision:
        raise ScheduleError("conflict", "配置已被修改，请刷新后重试")
    return task


def _run(tx, run_id: str) -> dict:
    run = tx.run(run_id)
    if run is None:
        raise ScheduleError("not_found", "运行记录不存在")
    return run


def _request_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ScheduleError("invalid_request", "请求标识应为 1 至 128 个字符")
    return value


def _new_run(task: dict, scheduled_for: float, now: float, key: str, *, preview: bool, trigger: str) -> dict:
    settings = ScheduleSettings.model_validate(task["settings"])
    run_id = "schedule_run_" + uuid.uuid4().hex
    return {"id": run_id, "task_id": task["id"], "revision": task["revision"], "request_key": key,
            "settings": task["settings"], "scheduled_for": scheduled_for, "created_at": now,
            "trigger": trigger, "preview": preview, "status": "queued", "cancel_requested": False,
            "gateway_run_id": run_id, "result_text": "", "receipts": [], "error_code": "",
            "started_at": None, "generated_at": None, "delivery_started_at": None, "finished_at": None,
            **research_input(settings, scheduled_for)}


def _summary(run: dict) -> dict:
    return {key: value for key, value in run.items() if key not in {"prompt", "result_text", "receipts", "request_key"}}
