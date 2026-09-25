"""GC lifecycle regression through the controller's current batch assembly."""

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.control_types import DispatchResult, WorkerState
from chatcopilot.harness.governance_types import GovernanceOptions
from chatcopilot.harness.models import HarnessError


class ObservedWorkers:
    def __init__(self):
        self.states = {}
        self.launches = []
        self.delivery_launches = []

    def observe(self, task):
        return self.states.get(task["task_id"], WorkerState.INACTIVE)

    def launch(self, task):
        self.launches.append(task["task_id"])
        self.states[task["task_id"]] = WorkerState.ACTIVE
        return DispatchResult("scheduled")

    def launch_delivery(self, task):
        self.delivery_launches.append(task["task_id"])
        return DispatchResult("scheduled")


class RecordedTaskOperations:
    """The Service's task execution port; persistence and lifecycle stay real."""

    def __init__(self, store, lifecycle):
        self.store = store
        self.lifecycle = lifecycle
        self.started = []
        self.lose_receipt = False

    def preflight_model(self, model, reasoning_effort):
        assert (model, reasoning_effort) == ("fixture", "medium")

    def start(self, run, sequence, options):
        key = f"{run['run_id']}-{sequence}"
        task, created = self.store.create({
            "task_id": "repair-" + key,
            "pipeline_version": 10,
            "request_key": key,
            "request_digest": key,
            "context_key": "repository",
            "match_key": key,
            "active_key": key,
            "source": {"kind": "code_health"},
            "options": asdict(options),
            "repository": run["repository"],
            "base_commit": "fixture-main",
            "unit": key,
            "governance_run_id": run["run_id"],
            "governance_sequence": sequence,
            "dispatch_state": "creating",
            "delivery": {"state": "pending"},
        })
        if created:
            self.started.append(task["task_id"])
        if self.lose_receipt:
            self.lose_receipt = False
            raise RuntimeError("creation receipt lost")
        return task

    def start_learning(self, run, sequence, options, source):
        raise AssertionError("fixture has no merged Skill lesson")

    def launch(self, task_id):
        with self.lifecycle.operation(task_id):
            self.lifecycle.launch_locked(task_id)

    def resume(self, task_id):
        with self.lifecycle.operation(task_id):
            self.lifecycle.prepare_resume_locked(task_id)
            self.lifecycle.claim_resume_locked(task_id)
            self.lifecycle.launch_locked(task_id)

    def retry_delivery(self, task_id):
        self.store.update(task_id, delivery_cancel_requested=False)


@pytest.fixture
def batch(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    workers = ObservedWorkers()
    controller = HarnessController(
        repository,
        root=tmp_path / "private",
        evaluator=Mock(),
        worker_control=workers,
    )
    service = controller._governance_runs()
    operations = RecordedTaskOperations(controller.store, controller.lifecycle)
    service.tasks = operations
    return SimpleNamespace(controller=controller, service=service, store=controller.store,
                           workers=workers, operations=operations)


def options(count=2):
    return GovernanceOptions("fixture", stop_condition={"mode": "findings", "count": count})


def test_runtime_assembly_injects_existing_lifecycle(batch):
    service = batch.controller._governance_runs()
    assert service.lifecycle is batch.controller.lifecycle
    assert service.lifecycle.workers is batch.workers
    assert service.tasks.controller is batch.controller
    assert service.store is batch.controller.store


def test_cancel_waits_for_worker_and_delivery_then_resume_requires_stop(batch):
    run = batch.service.start(options(), request_id="cancel-run")
    task_id = run["current_task_id"]
    batch.store.update(task_id, status="fixed", stage="done", delivery={"state": "waiting_checks"})

    cancelling = batch.service.cancel(run["run_id"])
    assert cancelling["status"] == "cancel_requested"
    assert batch.store.get(task_id)["delivery_cancel_requested"] is True
    batch.workers.states[task_id] = WorkerState.INACTIVE
    assert batch.service.advance(run["run_id"])["status"] == "cancel_requested"
    assert batch.workers.delivery_launches
    batch.store.update(task_id, delivery={"state": "cancelled"})
    cancelled = batch.service.advance(run["run_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["stop_reason"] == "cancelled"
    assert len(batch.operations.started) == 1

    batch.workers.states[task_id] = WorkerState.UNKNOWN
    with pytest.raises(HarnessError) as error:
        batch.service.resume(run["run_id"])
    assert error.value.code == "worker_unavailable"
    assert batch.service.get(run["run_id"])["status"] == "cancelled"


def test_lost_receipt_and_repeated_callbacks_keep_one_child_and_persist_counts(batch):
    batch.operations.lose_receipt = True
    run = batch.service.start(options(), request_id="lost-receipt")
    task_id = run["current_task_id"]
    assert run["status"] == "blocked"
    assert run["sequence"] == 1
    assert len(batch.operations.started) == 1

    restarted_service = batch.controller._governance_runs()
    restarted_service.tasks = batch.operations
    resumed = restarted_service.resume(run["run_id"])
    assert resumed["current_task_id"] == task_id
    assert len(batch.workers.launches) == 1
    for _ in range(2):
        assert restarted_service.advance(run["run_id"])["current_task_id"] == task_id
    assert len(batch.operations.started) == len(batch.workers.launches) == 1

    batch.workers.states[task_id] = WorkerState.INACTIVE
    batch.store.update(task_id, status="fixed", stage="done", governance_finding_id="finding-one",
                       accepted_candidate={"attempt": 1}, elapsed_seconds=12,
                       delivery={"state": "checks_pending"})
    waiting = restarted_service.advance(run["run_id"])
    assert waiting["status"] == "waiting_delivery"
    assert (waiting["found_count"], waiting["merged_count"], waiting["elapsed_seconds"]) == (1, 0, 12)
    assert waiting["sequence"] == 1
    assert len(batch.operations.started) == 1
    assert restarted_service.runs.get(run["run_id"])["found_count"] == 1

    batch.store.update(task_id, delivery={"state": "merged"})
    batch.workers.states[task_id] = WorkerState.ACTIVE
    assert restarted_service.advance(run["run_id"])["sequence"] == 1
    assert len(batch.operations.started) == 1
    batch.workers.states[task_id] = WorkerState.INACTIVE
    next_run = restarted_service.advance(run["run_id"])
    assert next_run["sequence"] == 2
    assert next_run["current_task_id"] != task_id
    assert (next_run["found_count"], next_run["merged_count"], next_run["elapsed_seconds"]) == (1, 1, 12)
    assert len(batch.operations.started) == 2
    assert len(restarted_service.runs.tasks(run["run_id"])) == 2
    persisted = batch.controller._governance_runs().get(run["run_id"])
    assert (persisted["sequence"], persisted["found_count"], persisted["merged_count"],
            persisted["elapsed_seconds"]) == (2, 1, 1, 12)

    second_id = next_run["current_task_id"]
    batch.workers.states[second_id] = WorkerState.INACTIVE
    batch.store.update(second_id, status="fixed", stage="done", governance_finding_id="finding-two",
                       accepted_candidate={"attempt": 1}, elapsed_seconds=5,
                       delivery={"state": "merged"})
    completed = restarted_service.advance(run["run_id"])
    assert (completed["status"], completed["stop_reason"]) == ("completed", "findings_limit")
    assert (completed["sequence"], completed["found_count"], completed["merged_count"],
            completed["elapsed_seconds"]) == (2, 2, 2, 17)
    assert len(batch.operations.started) == 2
