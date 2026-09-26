"""Behavioral contract for the code-health run projection across public callers."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.governance_run_service import GovernanceRuns
from chatcopilot.harness.governance_types import GovernanceOptions
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.task_budget import TaskBudget


def _controller(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    return HarnessController(
        repository,
        root=tmp_path / "private",
        evaluator=Mock(),
        worker_control=Mock(),
    )


def _run(controller, request_id, stop, status="running"):
    service = controller._governance_runs()
    run, created = service.runs.create(
        str(controller.repository),
        GovernanceOptions("fixture", stop_condition=stop).to_payload(),
        request_id,
    )
    assert created
    if status != "running":
        service.runs.update(run["run_id"], status=status)
    return run["run_id"]


def _task(controller, run_id, sequence, *, finding=None, delivery="merged", elapsed=0.0, learning=False):
    task_id = f"repair-{run_id}-{sequence}"
    task, created = controller.store.create({
        "task_id": task_id,
        "pipeline_version": 10,
        "request_key": task_id,
        "request_digest": task_id,
        "match_key": task_id,
        "context_key": str(controller.repository),
        "active_key": task_id,
        "source": {"kind": "code_health"},
        "repository": str(controller.repository),
        "options": {"timeout_seconds": 3},
        "governance_run_id": run_id,
        "governance_sequence": sequence,
        "base_commit": f"commit-{sequence}",
        "governance_summary": f"finding {sequence}",
        **({"skill_learning_origin": "repair-origin"} if learning else {}),
    })
    assert created
    controller.store.update(
        task_id,
        status="fixed" if delivery == "merged" else "queued",
        stage="done" if delivery == "merged" else "queued",
        governance_finding_id=finding,
        elapsed_seconds=elapsed,
        delivery={"state": delivery},
        message=f"task {sequence}",
        stop_reason="",
    )
    return task["task_id"]


def _stored_run(controller, run_id):
    with controller.store.database.connect() as connection:
        row = connection.execute(
            "SELECT payload FROM governance_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return json.loads(row[0])


def test_controller_detail_and_page_preserve_projection_filters_and_storage(tmp_path):
    controller = _controller(tmp_path)
    target = _run(controller, "first", {"mode": "findings", "count": 2}, status="completed")
    first = _task(controller, target, 1, finding="finding-1", elapsed=1.25)
    learning = _task(controller, target, 2, finding="learning-finding", elapsed=0.5, learning=True)
    latest = _task(controller, target, 3, delivery="pending", elapsed=2.25)
    blocked = _run(controller, "second", {"mode": "findings", "count": 2}, status="blocked")
    running = _run(controller, "third", {"mode": "findings", "count": 2})

    stored_before = _stored_run(controller, target)
    detail = controller.governance_run(target)
    assert (detail["found_count"], detail["merged_count"], detail["elapsed_seconds"]) == (1, 1, 4.0)
    assert (detail["current_task_id"], detail["sequence"]) == (latest, 3)
    assert [task["task_id"] for task in detail["tasks"]] == [first, learning, latest]
    assert [task["purpose"] for task in detail["tasks"]] == ["code_health", "skill_learning", "code_health"]
    assert set(detail["tasks"][0]) == {
        "task_id", "status", "stage", "base_commit", "governance_sequence", "governance_summary",
        "governance_finding_id", "message", "stop_reason", "elapsed_seconds", "delivery", "purpose",
    }
    assert detail["tasks"][1]["governance_finding_id"] == "learning-finding"

    page = controller.governance_runs(page=1, limit=1)
    assert page["total"] == 3 and page["runs"][0]["run_id"] == running
    assert controller.governance_runs(page=2, limit=1)["runs"][0]["run_id"] == blocked
    completed = controller.governance_runs(status="completed")
    assert completed == {"runs": [detail], "total": 1}
    assert controller.governance_runs(search=target, status="completed")["total"] == 1
    assert controller.governance_runs(search="absent")["total"] == 0
    with pytest.raises(ValueError):
        controller.governance_runs(status="not-a-run-state")
    assert _stored_run(controller, target) == stored_before
    with controller.store.database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_task_budget_keeps_fractional_time_across_children(tmp_path):
    controller = _controller(tmp_path)
    run_id = _run(controller, "budget", {"mode": "time", "seconds": 5})
    _task(controller, run_id, 1, finding="first", elapsed=2.25)
    current = _task(controller, run_id, 2, delivery="pending", elapsed=1.25)
    clock = [100.0]

    with TaskBudget(controller.store, current, clock=lambda: clock[0]) as budget:
        assert budget.remaining == pytest.approx(1.5)
        clock[0] += 1.5
        with pytest.raises(HarnessError) as error:
            budget.check()
        assert error.value.code == "budget_exhausted"

    assert controller.store.get(current)["elapsed_seconds"] == pytest.approx(2.75)
    assert controller.governance_run(run_id)["elapsed_seconds"] == pytest.approx(5.0)


def test_service_starts_next_item_once_then_stops_at_finding_limit(tmp_path):
    controller = _controller(tmp_path)
    run_id = _run(controller, "sequence", {"mode": "findings", "count": 2})
    _task(controller, run_id, 1, finding="first", elapsed=2)
    lifecycle = SimpleNamespace(
        workers=SimpleNamespace(observe=Mock(return_value=WorkerState.INACTIVE)),
        reconcile=Mock(),
    )
    launched = []

    def start_next(run, sequence, options):
        assert sequence == 2
        assert options.timeout_seconds is None
        task_id = _task(controller, run["run_id"], sequence, delivery="pending")
        return controller.store.get(task_id)

    port = SimpleNamespace(preflight_model=Mock(), start=Mock(side_effect=start_next),
                           launch=Mock(side_effect=launched.append))
    service = GovernanceRuns(controller.store, lifecycle, port, str(controller.repository))

    next_run = service.advance(run_id)
    assert next_run["sequence"] == 2
    assert next_run["current_task_id"] == launched[0]
    assert port.start.call_count == 1
    controller.store.update(launched[0], status="fixed", governance_finding_id="second",
                            delivery={"state": "merged"})
    finished = service.advance(run_id)
    assert (finished["status"], finished["stop_reason"]) == ("completed", "findings_limit")
    assert (finished["found_count"], finished["merged_count"]) == (2, 2)
    assert port.start.call_count == len(launched) == 1


def test_service_uses_fractional_remaining_time_and_stops_before_third_item(tmp_path):
    controller = _controller(tmp_path)
    run_id = _run(controller, "time-sequence", {"mode": "time", "seconds": 5})
    _task(controller, run_id, 1, finding="first", elapsed=2.25)
    lifecycle = SimpleNamespace(
        workers=SimpleNamespace(observe=Mock(return_value=WorkerState.INACTIVE)),
        reconcile=Mock(),
    )
    allocated = []

    def start_next(run, sequence, options):
        allocated.append((sequence, options.timeout_seconds))
        task_id = _task(controller, run["run_id"], sequence, delivery="pending")
        return controller.store.get(task_id)

    port = SimpleNamespace(preflight_model=Mock(), start=Mock(side_effect=start_next), launch=Mock())
    service = GovernanceRuns(controller.store, lifecycle, port, str(controller.repository))

    second = service.advance(run_id)
    assert allocated == [(2, 3)]
    controller.store.update(second["current_task_id"], status="fixed", governance_finding_id="second",
                            elapsed_seconds=2.75, delivery={"state": "merged"})
    finished = service.advance(run_id)
    assert (finished["status"], finished["stop_reason"]) == ("completed", "budget_exhausted")
    assert finished["elapsed_seconds"] == pytest.approx(5)
    assert port.start.call_count == 1
