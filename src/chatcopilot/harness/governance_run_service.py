"""Sequential GC policy over ordinary single-finding Harness tasks."""
from __future__ import annotations

import math
import re
import uuid

from chatcopilot.harness.config import safe_error
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.governance_run_repository import GovernanceRunRepository
from chatcopilot.harness.governance_types import GovernanceLifecyclePort, GovernanceOptions, GovernanceTaskPort, RUN_ACTIVE
from chatcopilot.harness.models import ACTIVE, HarnessError, RepairOptions


_DELIVERY_DONE = frozenset({"no_changes", "merged", "closed", "cancelled"})
_DELIVERY_FAILED = frozenset({"blocked", "paused", "checks_failed", "retryable", "closed", "cancelled"})
_TASK_FAILED = frozenset({"needs_review", "failed", "blocked", "interrupted", "cancelled"})


def project_run(run: dict, tasks: list[dict]) -> dict:
    """Keep batch policy in Service, including the query projection."""
    issues = [task for task in tasks if not task.get("skill_learning_origin")]
    def outcome(task):
        if task.get("governance_outcome") == "failed":
            return "failed"
        if task.get("delivery", {}).get("state") == "merged":
            return "merged"
        if task["status"] == "no_changes":
            return "no_changes"
        return "pending"

    values = {
        "found_count": sum(bool(task.get("governance_finding_id")) for task in issues),
        "merged_count": sum(outcome(task) == "merged" for task in issues),
        "failed_count": sum(outcome(task) == "failed" for task in issues),
        "elapsed_seconds": sum(float(task.get("elapsed_seconds", 0)) for task in tasks),
        "current_task_id": tasks[-1]["task_id"] if tasks else None,
        "sequence": len(tasks),
    }
    return {**run, **values, "tasks": [{
        **{key: task[key] for key in (
            "task_id", "status", "stage", "base_commit", "governance_sequence", "governance_summary", "governance_finding_id",
            "message", "stop_reason", "error_code", "elapsed_seconds", "delivery") if key in task},
        "purpose": "skill_learning" if task.get("skill_learning_origin") else "code_health",
        "outcome": outcome(task),
        **({"failure": task["governance_failure"]} if task.get("governance_failure") else {}),
    } for task in tasks]}


class GovernanceRuns:
    def __init__(self, store, lifecycle: GovernanceLifecyclePort, tasks: GovernanceTaskPort, repository: str):
        self.store, self.lifecycle, self.tasks, self.repository = store, lifecycle, tasks, repository
        self.runs = GovernanceRunRepository(store)

    def _project(self, run):
        return project_run(run, self.runs.tasks(run["run_id"]))

    def _refresh(self, run_id):
        value = self._project(self.runs.get(run_id))
        return self.runs.update(run_id, **{key: value[key] for key in (
            "current_task_id", "sequence", "found_count", "merged_count", "failed_count", "elapsed_seconds")})

    def get(self, run_id):
        run = self.runs.get(run_id)
        if run["repository"] != self.repository:
            raise HarnessError("not_found", "此仓库没有该熵回收批次")
        return self._project(run)

    def page(self, **kwargs):
        page = self.runs.page(repository=self.repository, **kwargs)
        return {**page, "runs": [self._project(run) for run in page["runs"]]}

    def release_safety_holds(self):
        """A failed batch stays failed; only verified shutdown releases repository occupancy."""
        for run in self.runs.failed(self.repository):
            task_id = run.get("current_task_id")
            if not task_id:
                continue
            try:
                task = self.store.get(task_id)
                if not task.get("governance_unsafe"):
                    continue
                self.lifecycle.reconcile(task_id)
                task = self.store.get(task_id)
                if (self.lifecycle.workers.observe(task) == WorkerState.INACTIVE
                        and task["status"] not in ACTIVE
                        and not task.get("current_evaluation_id") and not task.get("delivery_evaluation")
                        and task.get("delivery", {}).get("state") in _DELIVERY_DONE):
                    self.store.update(task_id, governance_unsafe=False)
            except Exception:
                # Keep the hold when the observation or persistence is uncertain.
                continue

    def start(self, options: GovernanceOptions, *, request_id=None):
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("invalid request ID")
        with self.runs.locked():
            existing = self.runs.by_request(request_id)
            if existing is None:
                if self.store.active_governance(self.repository):
                    raise HarnessError("governance_active", "仓库仍有活动或未确认停止的熵回收任务或交付")
                self.tasks.preflight_model(options.model, options.reasoning_effort, model_settings=options.model_settings, model_profile=options.model_profile)
            run, _ = self.runs.create(self.repository, options.to_payload(), request_id)
            self._advance(run["run_id"], preflighted=existing is None)
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

    def _fail_run(self, run_id, code, message):
        if code in {"worker_unavailable", "evaluation_unconfirmed", "delivery_unconfirmed", "inconsistent_result"}:
            current = self.runs.get(run_id).get("current_task_id")
            if current:
                self.store.update(current, governance_unsafe=True)
        self.runs.update(run_id, status="failed", stop_reason=code, message=message)

    def _settle_failed_item(self, run_id, task):
        """Return true only after no execution or automatic merge can outlive this item."""
        task_id = task["task_id"]
        state = task.get("delivery", {}).get("state")
        failure = task.get("governance_failure") or {
            "code": ((task.get("delivery", {}).get("error_code") or state or "delivery_failed")
                     if task["status"] == "fixed" else (task.get("error_code") or task["status"])),
            "message": (task.get("delivery", {}).get("message") if task["status"] == "fixed" else task.get("message"))
                       or "当前问题未能完成处理",
        }
        if state == "merged":
            self._fail_run(run_id, "inconsistent_result", "任务状态与已合并 PR 不一致；停止创建下一项")
            return False
        if not task.get("governance_failure"):
            self.store.update(task_id, governance_failure=failure)
        if state not in _DELIVERY_DONE:
            if state in _DELIVERY_FAILED or task["status"] in _TASK_FAILED:
                prior_cancel = bool(task.get("delivery_cancel_requested"))
                self.lifecycle.cancel(task_id)
                task = self.store.get(task_id)
                observed = self.lifecycle.workers.observe(task)
                if observed == WorkerState.UNKNOWN:
                    self._fail_run(run_id, "worker_unavailable", "取消交付后无法确认 worker 状态")
                    return False
                if observed != WorkerState.INACTIVE:
                    return False
                state = task.get("delivery", {}).get("state")
                if state not in _DELIVERY_DONE and prior_cancel:
                    self._fail_run(run_id, "delivery_unconfirmed", "交付取消后仍无法确认远端安全终态")
                    return False
            if state not in _DELIVERY_DONE:
                return False
        self.store.update(task_id, governance_outcome="failed", governance_failure=failure)
        return True

    def _advance(self, run_id, *, preflighted=False):
        from chatcopilot.harness.batch_graph import build_batch_graph
        from chatcopilot.harness.graph_repository import checkpoints
        from chatcopilot.harness.graph_service import execute_graph
        run = self._refresh(run_id)
        if run["status"] not in RUN_ACTIVE:
            return
        with checkpoints(self.store.root, "gc:" + run_id) as saver:
            execute_graph(build_batch_graph(self, saver), "gc:" + run_id,
                          initial={"preflighted": preflighted})

    def graph_node(self, name):
        def node(state):
            run_id = state["identity"].removeprefix("gc:")
            try:
                return {"phase": name, **getattr(self, "graph_" + name)(run_id, state)}
            except Exception as exc:
                current = self._refresh(run_id)
                if current["status"] != "cancel_requested" and current["current_task_id"]:
                    child = self.store.get(current["current_task_id"])
                    if (child.get("governance_run_id") == run_id
                            and child.get("governance_sequence") == current["sequence"]
                            and child.get("request_key") == f"{run_id}-{current['sequence']}"
                            and child["status"] == "queued" and child.get("dispatch_state") == "creating"):
                        self.runs.update(run_id, status="running", stop_reason="", message="核对已创建任务的派发")
                        return {"route": "wait", "child_id": child["task_id"]}
                self.runs.update(run_id, status="cancel_requested" if current["status"] == "cancel_requested" else "failed",
                                 stop_reason=getattr(exc, "code", "execution_error"), message=safe_error(exc))
                return {"route": "wait" if current["status"] == "cancel_requested" else "__end__"}
        return node

    def graph_observe(self, run_id, state):
        run = self._refresh(run_id)
        if run["status"] not in RUN_ACTIVE:
            return {"route": "__end__"}
        if run["status"] == "cancel_requested":
            return {"route": "cancel"}
        if not run["current_task_id"]:
            return {"route": "limits"}
        task = self.store.get(run["current_task_id"])
        observed = self.lifecycle.workers.observe(task)
        if observed == WorkerState.UNKNOWN:
            self._fail_run(run_id, "worker_unavailable", "无法确认当前 worker 已停止；未创建下一项")
            return {"route": "__end__"}
        if task["status"] == "queued" and task.get("dispatch_state") == "creating":
            return {"route": "launch" if observed == WorkerState.INACTIVE else "wait", "child_id": task["task_id"]}
        self.lifecycle.reconcile(task["task_id"])
        task = self.store.get(task["task_id"])
        observed = self.lifecycle.workers.observe(task)
        if observed == WorkerState.UNKNOWN:
            self._fail_run(run_id, "worker_unavailable", "对账后无法确认当前 worker 已停止；未创建下一项")
            return {"route": "__end__"}
        if task["status"] == "fixed":
            self.runs.update(run_id, status="waiting_delivery", message="等待当前问题 PR 合并")
        if observed != WorkerState.INACTIVE:
            return {"route": "wait"}
        if task.get("current_evaluation_id") or task.get("delivery_evaluation"):
            self._fail_run(run_id, "evaluation_unconfirmed", "验证尚未确认停止；未创建下一项")
            return {"route": "__end__"}
        if task["status"] in ACTIVE:
            return {"route": "wait"}
        delivery = task.get("delivery", {}).get("state")
        if task["status"] == "fixed" and delivery not in {"merged", *_DELIVERY_FAILED}:
            return {"route": "wait"}
        if task["source"].get("skill_learning"):
            return {"route": "settle"}
        if task["status"] == "no_changes":
            self.runs.update(run_id, status="completed", stop_reason="no_changes", message="未发现可执行问题；调查范围见当前任务")
            return {"route": "__end__"}
        if task["status"] == "fixed" and delivery == "merged":
            return {"route": "learning"}
        if not task.get("governance_finding_id"):
            self._fail_run(run_id, task.get("error_code") or "discovery_failed", task.get("message") or "调查未形成可计数的问题")
            return {"route": "__end__"}
        return {"route": "settle"}

    def graph_cancel(self, run_id, state):
        run = self._refresh(run_id)
        if run["current_task_id"]:
            self.lifecycle.cancel(run["current_task_id"])
            task = self.store.get(run["current_task_id"])
            if (self.lifecycle.workers.observe(task) != WorkerState.INACTIVE
                    or task["status"] in ACTIVE or task.get("current_evaluation_id") or task.get("delivery_evaluation")
                    or task.get("delivery", {}).get("state") not in {None, "cancelled", "closed", "merged", "no_changes"}):
                return {"route": "wait"}
        self.runs.update(run_id, status="cancelled", stop_reason="cancelled", message="整次熵回收已取消")
        return {"route": "__end__"}

    def graph_settle(self, run_id, state):
        run = self._refresh(run_id)
        task = self.store.get(run["current_task_id"])
        learning = task["source"].get("skill_learning")
        delivery = task.get("delivery", {}).get("state")
        if learning:
            if delivery in _DELIVERY_FAILED and delivery not in {"closed", "cancelled"}:
                if not self._settle_failed_item(run_id, task):
                    return {"route": "wait" if self.runs.get(run_id)["status"] in RUN_ACTIVE else "__end__"}
            result = "merged" if delivery == "merged" else "no_change" if task["status"] == "no_changes" else "failed"
            self.store.update(learning["origin_task_id"], skill_learning={"state": result, "task_id": task["task_id"]})
        elif not self._settle_failed_item(run_id, task):
            return {"route": "wait" if self.runs.get(run_id)["status"] in RUN_ACTIVE else "__end__"}
        return {"route": "limits"}

    def graph_learning(self, run_id, state):
        from dataclasses import asdict
        from chatcopilot.harness.artifact_repository import ArtifactRepository
        from chatcopilot.harness.skill_context import learning_source
        run = self._refresh(run_id)
        task = self.store.get(run["current_task_id"])
        source = learning_source(task, self.store.attempts(task["task_id"]))
        if not source or self.remaining(run) is not None and self.remaining(run) <= 0:
            return {"route": "limits", "kind": "code_health"}
        reference = asdict(ArtifactRepository(self.store.root / "jobs" / task["task_id"]).put("learning_source", 1, source))
        return {"route": "create", "kind": "skill_learning", "sequence": run["sequence"] + 1,
                "context_ref": reference, "child_id": task["task_id"], "preflighted": False}

    def graph_limits(self, run_id, state):
        run = self._refresh(run_id)
        if run["status"] == "cancel_requested":
            return self.graph_cancel(run_id, state)
        stop = run["options"]["stop_condition"]
        if stop["mode"] == "findings" and run["found_count"] >= stop["count"]:
            self.runs.update(run_id, status="completed", stop_reason="findings_limit",
                message=f"已发现 {run['found_count']} 项；已合并 {run['merged_count']} 项，处理失败 {run['failed_count']} 项")
            return {"route": "__end__"}
        remaining = self.remaining(run)
        if remaining is not None and remaining <= 0:
            self.runs.update(run_id, status="completed", stop_reason="budget_exhausted", message="累计执行时间已用完")
            return {"route": "__end__"}
        if self.store.active_governance(run["repository"]):
            raise HarnessError("governance_active", "仓库仍有未完成的代码熵回收任务或交付")
        return {"route": "create", "kind": "code_health", "sequence": run["sequence"] + 1}

    def graph_create(self, run_id, state):
        from chatcopilot.harness.artifact_repository import ArtifactRepository
        run = self._refresh(run_id)
        if run["status"] == "cancel_requested":
            return {"route": "wait"}
        options = GovernanceOptions.from_payload(run["options"])
        remaining = self.remaining(run)
        if not state.get("preflighted"):
            self.tasks.preflight_model(options.model, options.reasoning_effort, model_settings=options.model_settings, model_profile=options.model_profile)
        if state["kind"] == "skill_learning":
            source = ArtifactRepository(self.store.root / "jobs" / state["child_id"]).read(state["context_ref"])
            child_options = RepairOptions(options.model, options.reasoning_effort, 1, 1800, options.model_settings, options.model_profile)
            child = self.tasks.start_learning(run, state["sequence"], child_options, source)
            self.store.update(source["origin_task_id"], skill_learning={"state": "queued", "task_id": child["task_id"]})
        else:
            child_options = RepairOptions(options.model, options.reasoning_effort, options.max_attempts,
                math.ceil(remaining) if remaining is not None else None, options.model_settings, options.model_profile)
            child = self.tasks.start(run, state["sequence"], child_options)
        if child.get("governance_run_id") != run_id or child.get("governance_sequence") != state["sequence"]:
            raise HarnessError("governance_active", "创建结果不属于当前回收批次，未启动任务")
        self._refresh(run_id)
        self.runs.update(run_id, status="running", stop_reason="", message="逐项发现并处理")
        return {"route": "launch", "child_id": child["task_id"], "preflighted": False}

    def graph_launch(self, run_id, state):
        self.tasks.launch(state["child_id"])
        return {"route": "wait"}

    def cancel(self, run_id):
        with self.runs.locked():
            run = self.get(run_id)
            if run["status"] in {"completed", "cancelled", "failed"}:
                return run
            self.runs.update(run_id, status="cancel_requested", stop_reason="cancelled")
            self._advance(run_id)
            return self.get(run_id)

    def resume(self, run_id):
        with self.runs.locked():
            self.get(run_id)
            run = self._refresh(run_id)
            if run["status"] not in {"blocked", "cancelled"}:
                raise HarnessError("conflict", "只有历史停止或取消的回收批次可以恢复")
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
