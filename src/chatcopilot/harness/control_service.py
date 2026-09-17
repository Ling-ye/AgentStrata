"""Harness lifecycle decisions, independent of the concrete process supervisor."""
from __future__ import annotations

import logging
from typing import Any

from chatcopilot.harness.control_types import EVALUATION_TERMINAL_STATES, EvaluationControlPort, WorkerControlPort, WorkerState, external_evaluation_id
from chatcopilot.harness.models import ACTIVE, GOVERNANCE_VERSION, PIPELINE_VERSION, Cancelled, HarnessError
from chatcopilot.harness.store import HarnessStore


def check_cancellation(task: dict[str, Any]) -> None:
    if task["status"] in {"cancel_requested", "cancelled"}:
        raise Cancelled()


class HarnessLifecycle:
    def __init__(self, store: HarnessStore, workers: WorkerControlPort, evaluator: EvaluationControlPort) -> None:
        self.store, self.workers, self.evaluator = store, workers, evaluator

    def operation(self, task_id: str):
        return self.store.control_guard(task_id)

    @staticmethod
    def require_current(task: dict[str, Any]) -> None:
        if task.get("pipeline_version") != PIPELINE_VERSION:
            raise HarnessError("source_archived", "旧任务只读保留，请重新加载来源创建任务")

    def require_stopped(self, task: dict[str, Any]) -> None:
        state = self.workers.observe(task)
        if state == WorkerState.ACTIVE:
            raise HarnessError("conflict", "原 worker 尚未结束")
        if state == WorkerState.UNKNOWN:
            raise HarnessError("worker_unavailable", "无法确认原 worker 已停止，请稍后核对")

    def _require_evaluation_stopped(self, task: dict[str, Any]) -> None:
        if task.get("delivery_evaluation"):
            raise HarnessError("conflict", "交付复测尚未收尾，不能恢复或接续修复")
        ident = self._external_evaluation(task)
        if ident and not self._evaluation_stopped(ident):
            raise HarnessError("conflict", "原验证尚未确认结束")

    def prepare_resume_locked(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        self.require_current(task)
        if task["source"].get("kind") == "code_health":
            raise HarnessError("new_snapshot_required", "代码治理请重新启动，以当前源码创建新快照")
        self.require_stopped(task)
        task = self._reconcile_locked(task_id)
        self._require_evaluation_stopped(task)
        if task["source"].get("kind") == "evaluation" and task["source"].get("result_schema_version") != 2:
            raise HarnessError("source_archived", "旧测评来源已归档，请使用新测评发起修复")
        if task["source"].get("blockers"):
            raise HarnessError("source_incomplete", "来源证据不完整，补充观测后重新加载并发起")
        if task["status"] not in {"blocked", "interrupted", "cancelled", "waiting_input"}:
            raise HarnessError("conflict", "此任务尚未停止或已完成，不能恢复")
        if task["status"] == "waiting_input" and not task["source"].get("image_resources"):
            raise HarnessError("image_required", "请先补充原图，系统会自动继续")
        return task

    def prepare_continuation_locked(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        self.require_current(task)
        self.require_stopped(task)
        task = self._reconcile_locked(task_id)
        self._require_evaluation_stopped(task)
        if task["status"] in ACTIVE:
            raise HarnessError("conflict", "原任务仍有活动执行，不能创建接续任务")
        return task

    def claim_resume_locked(self, task_id: str) -> tuple[dict[str, Any], bool]:
        task, claimed = self.store.claim_resume(task_id)
        if claimed and task.get("delivery"):
            task = self.store.update(task_id, delivery_cancel_requested=False,
                delivery={**task["delivery"], "state": "pending", "error_code": None})
        return task, claimed

    def launch_locked(self, task_id: str) -> None:
        task = self.store.get(task_id)
        self.require_current(task)
        if task["source"].get("kind") == "code_health" and task["source"].get("governance_version") != GOVERNANCE_VERSION:
            raise HarnessError("new_snapshot_required", "旧治理任务保留历史，请重新创建源码快照")
        if task["status"] != "queued":
            return
        result = self.workers.launch(task)
        values: dict[str, Any] = {"dispatch_state": result.state}
        if result.message:
            values["message"] = result.message
        if result.state == "failed":
            values.update(status="blocked", error_code=result.code)
        self.store.update(task_id, if_status=frozenset({"queued", "running"}), **values)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self.operation(task_id):
            task = self.store.get(task_id)
            self.require_current(task)
            task = self.store.update(task_id, delivery_cancel_requested=True)
            if task.get("delivery_evaluation"):
                self.delivery_locked(task_id, "cancel")
                return self.store.get(task_id)
            if task["status"] in {*ACTIVE, "waiting_input"} or task.get("current_evaluation_id"):
                task = self.store.update(task_id,
                    if_status=frozenset({*ACTIVE, "waiting_input", "blocked", "interrupted"}),
                    status="cancel_requested")
            if task["status"] == "cancel_requested":
                task = self._reconcile_locked(task_id)
            if task["status"] not in ACTIVE and task.get("delivery"):
                self.delivery_locked(task_id, "cancel")
            return self.store.get(task_id)

    @staticmethod
    def _external_evaluation(task: dict[str, Any]) -> str | None:
        return external_evaluation_id(task["source"], task.get("current_evaluation_id"))

    def _cancel_evaluation(self, task: dict[str, Any]) -> bool:
        ident = self._external_evaluation(task)
        if ident:
            if self._evaluation_stopped(ident):
                return True
            # Cancellation acknowledgement is not a terminal result.
            try:
                self.evaluator.cancel(ident)
            except Exception:
                if self._evaluation_stopped(ident):
                    return True
                raise
            if not self._evaluation_stopped(ident):
                return False
        return True

    def _evaluation_stopped(self, ident: str) -> bool:
        return self.evaluator.execution_status(ident) in EVALUATION_TERMINAL_STATES

    def _reconcile_locked(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        self.require_current(task)
        if task["status"] not in ACTIVE or task.get("delivery_evaluation"):
            return task
        if task["status"] == "cancel_requested":
            external_stopped = self._cancel_evaluation(task)
        else:
            external_stopped = True
        state = self.workers.observe(task)
        if state != WorkerState.INACTIVE or not external_stopped:
            return self.store.get(task_id)
        # Worker completion may have raced the probe. Repo conditionally updates
        # the fresh record and preserves any completed outcome.
        if task["status"] == "cancel_requested":
            return self.store.update(task_id, if_status=frozenset({"cancel_requested"}),
                status="cancelled", stage="done", current_evaluation_id=None, message="修复任务已取消")
        return self.store.interrupt(task_id, if_status=frozenset({"queued", "running"}))

    def reconcile(self, task_id: str) -> dict[str, Any]:
        with self.operation(task_id):
            return self._reconcile_locked(task_id)

    def reconcile_active(self) -> None:
        for task_id in self.store.active_tasks(PIPELINE_VERSION):
            try:
                self.reconcile(task_id)
            except Exception:
                logging.getLogger(__name__).warning("Harness lifecycle reconciliation unavailable for %s", task_id)

    def delivery_locked(self, task_id: str, action: str) -> None:
        task = self.store.get(task_id)
        self.require_current(task)
        if not task.get("delivery"):
            raise HarnessError("source_archived", "旧任务只读保留")
        if task["status"] in {*ACTIVE, "waiting_input"}:
            raise HarnessError("conflict", "修复尚未结束")
        self.store.update(task_id, delivery_request=action)
        # An existing repair/delivery worker will finish or the timer retries.
        # Do not launch a second process while its existence is unknown.
        if self.workers.observe(task) != WorkerState.INACTIVE:
            return
        result = self.workers.launch_delivery(task)
        if result.state == "failed":
            raise HarnessError(result.code, result.message)

    def delivery(self, task_id: str, action: str) -> dict[str, Any]:
        with self.operation(task_id):
            self.delivery_locked(task_id, action)
            return self.store.get(task_id)
