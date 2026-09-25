"""Sequential GC policy over ordinary single-finding Harness tasks."""
from __future__ import annotations

import math
import re
import uuid

from chatcopilot.harness.config import safe_error
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.governance_run_repository import GovernanceRunRepository
from chatcopilot.harness.governance_types import GovernanceOptions, GovernanceTaskPort, RUN_ACTIVE
from chatcopilot.harness.models import ACTIVE, HarnessError, RepairOptions


class GovernanceRuns:
    def __init__(self, store, lifecycle, tasks: GovernanceTaskPort, repository: str):
        self.store, self.lifecycle, self.tasks, self.repository = store, lifecycle, tasks, repository
        self.runs = GovernanceRunRepository(store)

    def get(self, run_id):
        run = self.runs.get(run_id)
        if run["repository"] != self.repository:
            raise HarnessError("not_found", "此仓库没有该熵回收批次")
        return self.runs.project(run)

    def start(self, options: GovernanceOptions, *, request_id=None):
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("invalid request ID")
        with self.runs.locked():
            run, _ = self.runs.create(self.repository, options.to_payload(), request_id)
            self._advance(run["run_id"])
            return self.get(run["run_id"])

    def advance(self, run_id):
        with self.runs.locked():
            self.get(run_id)
            self._advance(run_id)
            return self.get(run_id)

    @staticmethod
    def remaining(run):
        stop = run["options"]["stop_condition"]
        return max(0, stop["seconds"] - run["elapsed_seconds"]) if stop["mode"] == "time" else None

    def _advance(self, run_id):
        run = self.runs.refresh(run_id)
        if run["status"] not in RUN_ACTIVE:
            return
        try:
            task = self.store.get(run["current_task_id"]) if run["current_task_id"] else None
            if run["status"] == "cancel_requested":
                if task:
                    self.lifecycle.cancel(task["task_id"])
                    task = self.store.get(task["task_id"])
                    if (self.lifecycle.workers.observe(task) != WorkerState.INACTIVE
                            or task["status"] in ACTIVE or task.get("current_evaluation_id") or task.get("delivery_evaluation")
                            or task.get("delivery", {}).get("state") not in {None, "cancelled", "closed", "merged", "no_changes"}):
                        return
                self.runs.update(run_id, status="cancelled", stop_reason="cancelled", message="整次熵回收已取消")
                return
            if task:
                # A crash between recording the child and dispatching it must not
                # turn into a second child or a false worker-loss conclusion.
                observed = self.lifecycle.workers.observe(task)
                if task["status"] == "queued" and task.get("dispatch_state") == "creating":
                    if observed == WorkerState.INACTIVE:
                        self.tasks.launch(task["task_id"])
                    return
                self.lifecycle.reconcile(task["task_id"])
                task = self.store.get(task["task_id"])
                delivery = task.get("delivery", {}).get("state")
                learning = task["source"].get("skill_learning")
                if task["status"] == "fixed":
                    self.runs.update(run_id, status="waiting_delivery",
                        message="等待 Skill PR 合并" if learning else "等待当前问题 PR 合并")
                if observed != WorkerState.INACTIVE or task.get("current_evaluation_id") or task.get("delivery_evaluation"):
                    return
                if task["status"] in ACTIVE:
                    return
                if learning:
                    if task["status"] == "fixed" and delivery not in {"merged", "blocked", "paused", "checks_failed", "retryable", "closed", "cancelled"}:
                        return
                    state = ("merged" if delivery == "merged" else "no_change" if task["status"] == "no_changes" else "failed")
                    self.store.update(learning["origin_task_id"], skill_learning={"state": state, "task_id": task["task_id"]})
                else:
                    if task["status"] == "no_changes":
                        self.runs.update(run_id, status="completed", stop_reason="no_changes", message="未发现可执行问题；调查范围见当前任务")
                        return
                    if task["status"] != "fixed" or delivery in {"blocked", "paused", "checks_failed", "retryable", "closed", "cancelled"}:
                        self.runs.update(run_id, status="blocked", stop_reason=task.get("error_code") or delivery or task["status"],
                            message=task.get("delivery", {}).get("message") if task["status"] == "fixed" else task.get("message") or "当前问题未完成，已停止继续发现")
                        return
                    if delivery != "merged":
                        return
                    from chatcopilot.harness.skill_context import learning_source
                    source = learning_source(task, self.store.attempts(task["task_id"]))
                    if source:
                        options = GovernanceOptions.from_payload(run["options"])
                        child_options = RepairOptions(options.model, options.reasoning_effort, 1, 1800)
                        child = self.tasks.start_learning(run, run["sequence"] + 1, child_options, source)
                        if child.get("governance_run_id") != run_id or child.get("governance_sequence") != run["sequence"] + 1:
                            raise HarnessError("governance_active", "Skill 学习任务不属于当前回收批次")
                        self.store.update(task["task_id"], skill_learning={"state": "queued", "task_id": child["task_id"]})
                        self.runs.refresh(run_id)
                        self.runs.update(run_id, status="running", stop_reason="", message="核对已合并改善的可复用教训")
                        self.tasks.launch(child["task_id"])
                        return
            run = self.runs.refresh(run_id)
            stop = run["options"]["stop_condition"]
            if stop["mode"] == "findings" and run["found_count"] >= stop["count"]:
                self.runs.update(run_id, status="completed", stop_reason="findings_limit", message="已完成设定数量的问题及 PR 交付")
                return
            remaining = self.remaining(run)
            if remaining is not None and remaining <= 0:
                self.runs.update(run_id, status="completed", stop_reason="budget_exhausted", message="累计执行时间已用完")
                return
            busy = self.store.active_governance(run["repository"])
            if busy:
                raise HarnessError("governance_active", "仓库仍有未完成的代码熵回收任务或交付")
            options = GovernanceOptions.from_payload(run["options"])
            child_options = RepairOptions(options.model, options.reasoning_effort, options.max_attempts,
                                          math.ceil(remaining) if remaining is not None else None)
            child = self.tasks.start(run, run["sequence"] + 1, child_options)
            if child.get("governance_run_id") != run_id or child.get("governance_sequence") != run["sequence"] + 1:
                raise HarnessError("governance_active", "创建结果不属于当前回收批次，未启动任务")
            self.runs.refresh(run_id)
            self.runs.update(run_id, status="running", stop_reason="", message="逐项发现并修复中")
            self.tasks.launch(child["task_id"])
        except Exception as exc:
            # Cancellation intent survives uncertain dispatch/cancellation receipts.
            current = self.runs.get(run_id)
            self.runs.update(run_id, status="cancel_requested" if current["status"] == "cancel_requested" else "blocked",
                stop_reason=getattr(exc, "code", "execution_error"), message=safe_error(exc))

    def cancel(self, run_id):
        with self.runs.locked():
            run = self.get(run_id)
            if run["status"] in {"completed", "cancelled"}:
                return self.get(run_id)
            self.runs.update(run_id, status="cancel_requested", stop_reason="cancelled")
            self._advance(run_id)
            return self.get(run_id)

    def resume(self, run_id):
        with self.runs.locked():
            self.get(run_id)
            run = self.runs.refresh(run_id)
            if run["status"] not in {"blocked", "cancelled"}:
                raise HarnessError("conflict", "只有停止的回收批次可以恢复")
            if self.runs.active(run["repository"]):
                raise HarnessError("governance_active", "仓库已有另一活动回收批次")
            busy = self.store.active_governance(run["repository"])
            if busy and busy != run["current_task_id"]:
                raise HarnessError("governance_active", "仓库还有另一项未完成的熵回收交付")
            remaining = self.remaining(run)
            if remaining is not None and remaining <= 0:
                raise HarnessError("budget_exhausted", "累计预算已经用完，恢复不会重置预算")
            task = self.store.get(run["current_task_id"]) if run["current_task_id"] else None
            if task:
                self.lifecycle.require_stopped(task)
                if task["status"] in {"needs_review", "failed", "no_changes"}:
                    raise HarnessError("conflict", "当前问题已终止或需要人工判断，请查看证据后重新发起")
            self.runs.update(run_id, status="running", stop_reason="", message="恢复原批次，累计消耗不重置")
            try:
                if task and task["status"] == "fixed" and task.get("delivery", {}).get("state") != "merged":
                    self.tasks.retry_delivery(task["task_id"])
                elif task and task["status"] in {"blocked", "interrupted", "cancelled"}:
                    self.tasks.resume(task["task_id"])
                self._advance(run_id)
            except Exception as exc:
                self.runs.update(run_id, status="blocked", stop_reason=getattr(exc, "code", "execution_error"), message=safe_error(exc))
                raise
            return self.get(run_id)
