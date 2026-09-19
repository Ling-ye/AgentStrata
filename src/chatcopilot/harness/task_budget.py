"""Account active execution segments; gaps between workers consume no budget."""
from __future__ import annotations

import time

from chatcopilot.harness.control_service import check_cancellation
from chatcopilot.harness.models import Cancelled, HarnessError


class TaskBudget:
    def __init__(self, store, task_id, *, enabled=True, clock=None):
        self.store, self.task_id, self.enabled = store, task_id, enabled
        self.clock = clock or time.monotonic

    def __enter__(self):
        if not self.enabled:
            return self
        task = self.store.get(self.task_id)
        self.elapsed = float(task.get("elapsed_seconds", 0))
        self.limit = task["options"]["timeout_seconds"]
        self.started = self.clock()
        self.last_saved = self.started
        self.run_id = task.get("governance_run_id")
        self.runs = None
        if self.run_id:
            from chatcopilot.harness.governance_run_repository import GovernanceRunRepository
            self.runs = GovernanceRunRepository(self.store)
            run = self.runs.project(self.runs.get(self.run_id))
            stop = run["options"]["stop_condition"]
            if stop["mode"] == "time":
                # Preserve sub-second remainder across child allocations rather
                # than granting another rounded second for each new problem.
                self.limit = max(0, stop["seconds"] - (run["elapsed_seconds"] - self.elapsed))
        return self

    @property
    def used(self):
        return self.elapsed + max(0, self.clock() - self.started)

    @property
    def remaining(self):
        return max(0, self.limit - self.used) if self.limit is not None else None

    def check(self):
        if not self.enabled:
            return
        task = self.store.control_state(self.task_id)
        check_cancellation(task)
        if task.get("delivery_cancel_requested"):
            raise Cancelled()
        if self.runs:
            run = self.runs.get(self.run_id)
            if run["status"] in {"cancel_requested", "cancelled"}:
                raise Cancelled()
            if run["status"] not in {"running", "waiting_delivery"}:
                raise HarnessError("governance_run_stopped", "整次回收已停止，不能继续主动执行")
        if self.remaining is not None and self.remaining <= 0:
            raise HarnessError("budget_exhausted", "累计执行时间预算已用完")
        if self.clock() - self.last_saved >= 5:
            self.save()

    def save(self):
        self.store.update(self.task_id, elapsed_seconds=self.used, remaining_seconds=self.remaining,
                          heartbeat_at=time.time())
        self.last_saved = self.clock()

    def __exit__(self, *_args):
        if self.enabled:
            self.save()
