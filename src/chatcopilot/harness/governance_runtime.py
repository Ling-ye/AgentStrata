"""Bind sequential GC policy to the existing controller and workers."""
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.governance_run_service import GovernanceRuns


class GovernanceTasks:
    def __init__(self, controller):
        self.controller = controller

    def preflight_model(self, model, reasoning_effort):
        from chatcopilot.harness.codex_adapter import preflight_worker_model
        preflight_worker_model(self.controller.settings, self.controller.repository, model, reasoning_effort)

    def start(self, run, sequence, options):
        return self.controller._start_code_health_task(options,
            request_id=f"{run['run_id']}-{sequence}", run_id=run["run_id"], sequence=sequence, launch=False)

    def start_learning(self, run, sequence, options, source):
        return self.controller._start_code_health_task(options,
            request_id=f"{run['run_id']}-{sequence}", run_id=run["run_id"], sequence=sequence,
            launch=False, learning_from=source)

    def launch(self, task_id):
        lifecycle = self.controller.lifecycle
        with lifecycle.operation(task_id):
            task = self.controller.store.get(task_id)
            if lifecycle.workers.observe(task) == WorkerState.INACTIVE:
                lifecycle.launch_locked(task_id)

    def resume(self, task_id):
        self.controller._resume_task(task_id)

    def retry_delivery(self, task_id):
        lifecycle = self.controller.lifecycle
        with lifecycle.operation(task_id):
            self.controller.store.update(task_id, delivery_cancel_requested=False)
            lifecycle.delivery_locked(task_id, "retry")


def governance_runs(controller):
    return GovernanceRuns(controller.store, controller.lifecycle, GovernanceTasks(controller), str(controller.repository))


def reconcile_runs(controller):
    service = governance_runs(controller)
    for run in service.runs.active(str(controller.repository)):
        try:
            service.advance(run["run_id"])
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Code-health batch reconciliation unavailable for %s", run["run_id"])
