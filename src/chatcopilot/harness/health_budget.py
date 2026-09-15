"""Code-health deadlines with an explicit owner for every execution timeout."""
from __future__ import annotations

import math
import subprocess
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Callable

from chatcopilot.harness.models import CodeHealthOptions, HarnessError


class HealthBudget:
    def __init__(self, options: CodeHealthOptions) -> None:
        self.options = options
        self.started = time.monotonic()
        self.deadline = (self.started + options.budget["seconds"]
                         if options.budget["mode"] == "time" else None)

    def check(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise HarnessError("budget_exhausted", f"代码治理已达到总时限 {self.options.budget['seconds']} 秒")

    @contextmanager
    def step(self, label: str, cancel: Callable[[], None]):
        cancel()
        self.check()
        now = time.monotonic()
        deadline = now + self.options.step_timeout_seconds
        code = "step_timeout"
        if self.deadline is not None and self.deadline <= deadline:
            deadline, code = self.deadline, "budget_exhausted"
        seconds = max(1, math.ceil(deadline - now))

        def expired() -> HarnessError:
            limit = self.options.budget["seconds"] if code == "budget_exhausted" else self.options.step_timeout_seconds
            reason = "任务总时限" if code == "budget_exhausted" else "单次执行时限"
            error = HarnessError(code, f"{label}：已达{reason} {limit} 秒")
            error.details = {"operation": label, "limit_kind": code, "limit_seconds": limit,
                             "allocated_seconds": seconds}
            return error

        def poll() -> None:
            # Cancellation retains precedence; timeouts retain the allocated owner.
            try:
                cancel()
            except HarnessError as exc:
                if exc.code == "budget_exhausted":
                    raise expired() from exc
                raise
            if time.monotonic() >= deadline:
                raise expired()

        try:
            yield seconds, poll
            poll()
        except subprocess.TimeoutExpired as exc:
            raise expired() from exc


class BudgetedCoder:
    """Apply the same step contract to audit, preparation, coding and review."""
    def __init__(self, coder, budget: HealthBudget) -> None:
        self.coder, self.budget = coder, budget

    def _call(self, method, root, evidence, options, output, cancel):
        with self.budget.step(method, cancel) as (seconds, poll):
            return getattr(self.coder, method)(root, evidence, replace(options, timeout_seconds=seconds), output, poll)

    def audit(self, *args):
        return self._call("audit", *args)

    def prepare(self, *args):
        return self._call("prepare", *args)

    def run(self, *args):
        return self._call("run", *args)

    def review(self, *args):
        return self._call("review", *args)
