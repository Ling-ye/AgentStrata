from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
import subprocess
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.code_health import HealthRun
from chatcopilot.harness.control_types import DispatchResult, WorkerState
from chatcopilot.harness.models import Cancelled, HarnessError, PIPELINE_VERSION
from chatcopilot.harness.worker_runtime import SystemdWorkerControl


class Workers:
    def __init__(self, state=WorkerState.INACTIVE):
        self.state = state
        self.launches = []
        self.delivery_launches = []
        self.probes = []

    def observe(self, task):
        self.probes.append(task["task_id"])
        return self.state

    def launch(self, task):
        self.launches.append(task["task_id"])
        self.state = WorkerState.ACTIVE
        return DispatchResult("scheduled")

    def launch_delivery(self, task):
        self.delivery_launches.append(task["task_id"])
        return DispatchResult("scheduled")


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr("chatcopilot.harness.api.configuration", lambda: {})
    workers = Workers()
    evaluator = SimpleNamespace(cancel=Mock(), execution_status=Mock(return_value="completed"))
    controller = HarnessController(tmp_path, root=tmp_path / "private", evaluator=evaluator, worker_control=workers)
    return controller, workers, evaluator


def task(controller, *, status="running", dispatch="unknown", **fields):
    ident = "repair-" + uuid.uuid4().hex
    controller.store.create({"task_id": ident, "pipeline_version": PIPELINE_VERSION,
        "request_key": ident, "request_digest": ident, "match_key": ident, "context_key": ident,
        "active_key": ident, "source": {"kind": "robot_task", "bot_id": "fixture", "run_id": "run-fixture"},
        "unit": "agentstrata-harness-" + ident[7:], "dispatch_state": dispatch,
        "options": {"model": "fixture"}, **fields})
    controller.store.update(ident, status=status)
    return ident


def occupied(controller, ident):
    with controller.store.database.connect() as connection:
        return connection.execute("SELECT active_key FROM tasks WHERE task_id=?", (ident,)).fetchone()[0] is not None


@pytest.mark.parametrize("state", [WorkerState.ACTIVE, WorkerState.UNKNOWN])
@pytest.mark.parametrize("dispatch", ["unknown", "scheduled", "creating"])
def test_cancel_keeps_occupancy_until_worker_stop_is_confirmed(control, state, dispatch):
    controller, workers, _ = control
    ident = task(controller, dispatch=dispatch)
    workers.state = state
    assert controller.cancel(ident)["status"] == "cancel_requested"
    assert occupied(controller, ident)
    assert workers.probes == [ident]
    worker = HealthRun.__new__(HealthRun)
    worker.store, worker.ident = controller.store, ident
    with pytest.raises(Cancelled):
        worker.cancel()
    workers.state = WorkerState.INACTIVE
    assert controller.reconcile(ident)["status"] == "cancelled"
    assert not occupied(controller, ident)


def test_completed_cancellation_cannot_be_revived_by_late_worker(control):
    controller, _, _ = control
    ident = task(controller)
    controller.cancel(ident)
    for state in ("running", "fixed"):
        with pytest.raises(Cancelled):
            controller.store.update(ident, status=state)
    worker = HealthRun.__new__(HealthRun)
    worker.store, worker.ident = controller.store, ident
    with pytest.raises(Cancelled):
        worker.cancel()


@pytest.mark.parametrize("source,suffix", [({"kind": "evaluation"}, ""),
    ({"kind": "robot_task", "test_sha256": "test", "agent_source": {"case_ids": ["one"]}}, "-agent")])
def test_external_cancel_failure_and_pending_ack_keep_binding(control, source, suffix):
    controller, _, evaluator = control
    ident = task(controller, source=source, current_evaluation_id="eval-test")
    evaluator.execution_status.return_value = "running"
    evaluator.cancel.side_effect = RuntimeError("offline fixture")
    with pytest.raises(RuntimeError):
        controller.cancel(ident)
    assert controller.store.get(ident)["current_evaluation_id"] == "eval-test"
    assert occupied(controller, ident)
    evaluator.cancel.side_effect = None
    evaluator.execution_status.return_value = "running"
    assert controller.reconcile(ident)["status"] == "cancel_requested"
    assert occupied(controller, ident)
    evaluator.execution_status.return_value = "completed"
    assert controller.reconcile(ident)["status"] == "cancelled"
    evaluator.cancel.assert_called_with("eval-test" + suffix)
    assert controller.store.get(ident)["current_evaluation_id"] is None


def test_local_test_receipt_is_not_sent_to_external_evaluation(control):
    controller, _, evaluator = control
    ident = task(controller, current_evaluation_id="local-check",
        source={"kind": "robot_task", "test_sha256": "fixture"})
    assert controller.cancel(ident)["status"] == "cancelled"
    evaluator.cancel.assert_not_called()


def test_worker_completion_wins_over_stale_reconciliation(control):
    controller, workers, _ = control
    ident = task(controller)

    def completed(_task):
        controller.store.update(ident, status="fixed")
        return WorkerState.INACTIVE

    workers.observe = completed
    assert controller.reconcile(ident)["status"] == "fixed"


def test_cancel_preserves_already_completed_result(control):
    controller, _, _ = control
    ident = task(controller, status="fixed")
    assert controller.cancel(ident)["status"] == "fixed"


def test_cancellation_arriving_during_probe_is_not_changed_to_interrupted(control):
    controller, workers, _ = control
    ident = task(controller)

    def cancel_during_probe(_task):
        controller.store.update(ident, status="cancel_requested")
        return WorkerState.INACTIVE

    workers.observe = cancel_during_probe
    assert controller.reconcile(ident)["status"] == "cancel_requested"
    assert occupied(controller, ident)


@pytest.mark.parametrize("result,status", [(DispatchResult("scheduled"), "queued"),
    (DispatchResult("unknown"), "queued"), (DispatchResult("failed", "fixture", "failed"), "blocked")])
def test_dispatch_receipt_does_not_invent_worker_completion(control, result, status):
    controller, workers, _ = control
    ident = task(controller, status="queued", dispatch="creating")
    workers.launch = lambda _: result
    with controller.lifecycle.operation(ident):
        controller.lifecycle.launch_locked(ident)
    record = controller.store.get(ident)
    assert record["status"] == status and record["dispatch_state"] == result.state
    assert occupied(controller, ident) == (status == "queued")


@pytest.mark.parametrize("terminal", ["completed", "partial", "interrupted", "error", "not_found"])
def test_cancel_does_not_resend_to_terminal_evaluation(control, terminal):
    controller, _, evaluator = control
    ident = task(controller, source={"kind": "evaluation"}, current_evaluation_id="eval-terminal")
    evaluator.execution_status.return_value = terminal
    assert controller.cancel(ident)["status"] == "cancelled"
    evaluator.cancel.assert_not_called()


def test_external_completion_racing_cancel_rejection_keeps_terminal_evidence(control):
    controller, _, evaluator = control
    ident = task(controller, source={"kind": "evaluation"}, current_evaluation_id="eval-terminal")
    evaluator.execution_status.side_effect = ["running", "completed"]
    evaluator.cancel.side_effect = RuntimeError("already completed")
    assert controller.cancel(ident)["status"] == "cancelled"


@pytest.mark.parametrize("failure,code", [(True, "evaluation_unavailable"), (False, "result_pending")])
def test_evaluation_worker_preserves_retryable_cancellation(tmp_path, monkeypatch, failure, code):
    from chatcopilot.evals.service import EvaluationServiceUnavailable
    from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
    client = SimpleNamespace(get=Mock(return_value={"status": "running"}),
        cancel=Mock(side_effect=EvaluationServiceUnavailable("fixture offline") if failure else None))
    evaluator = ServiceEvaluator(client)
    monkeypatch.setattr("chatcopilot.harness.evaluation_adapter.source_manifest", lambda _: {})
    cancellation = Mock(side_effect=[None, Cancelled()])
    with pytest.raises(HarnessError) as error:
        evaluator.run({"source": {"request": {}, "repetitions": 1}}, tmp_path, "eval-active", ["one"], cancellation)
    assert error.value.code == code
    client.cancel.assert_called_once_with("eval-active")


def test_queries_do_not_probe_worker_or_change_task(control):
    controller, workers, _ = control
    ident = task(controller)
    before = controller.store.get(ident)
    workers.observe = Mock(side_effect=AssertionError("query probed worker"))
    controller.store.interrupt = Mock(side_effect=AssertionError("query changed state"))
    controller.store.update = Mock(side_effect=AssertionError("query changed state"))
    assert controller.get(ident)["status"] == "running"
    assert controller.list()["tasks"][0]["status"] == "running"
    controller.progress(ident)
    assert controller.store.get(ident) == before


def test_cancel_serializes_with_inflight_launch(control):
    controller, workers, _ = control
    ident = task(controller, status="queued", dispatch="creating")
    entered, release, cancelled = Event(), Event(), Event()

    def launch(_task):
        entered.set()
        assert release.wait(3)
        workers.state = WorkerState.ACTIVE
        return DispatchResult("scheduled")

    workers.launch = launch

    def dispatch():
        with controller.lifecycle.operation(ident):
            controller.lifecycle.launch_locked(ident)

    def cancel():
        result = controller.cancel(ident)
        cancelled.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        launching = pool.submit(dispatch)
        assert entered.wait(3)
        cancelling = pool.submit(cancel)
        try:
            assert not cancelled.wait(.1)
        finally:
            release.set()
        launching.result(timeout=3)
        assert cancelling.result(timeout=3)["status"] == "cancel_requested"
    assert occupied(controller, ident)


@pytest.mark.parametrize("state", [WorkerState.ACTIVE, WorkerState.UNKNOWN])
def test_resume_and_continuation_require_confirmed_stop(control, state):
    controller, workers, _ = control
    ident = task(controller, status="interrupted")
    workers.state = state
    before = controller.store.get(ident)
    for operation in (controller.resume, controller.continue_task):
        with pytest.raises(HarnessError) as error:
            operation(ident)
        assert error.value.code == ("conflict" if state == WorkerState.ACTIVE else "worker_unavailable")
    assert controller.store.get(ident) == before
    assert not workers.launches


def test_concurrent_resume_launches_once(control):
    controller, workers, _ = control
    ident = task(controller, status="interrupted")

    def resume():
        try:
            return controller.resume(ident)["status"]
        except HarnessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resume(), range(2)))
    assert sorted(results) == ["conflict", "queued"]
    assert workers.launches == [ident]


def test_reconciliation_cannot_interrupt_a_concurrent_resume(control):
    controller, workers, _ = control
    ident = task(controller, status="interrupted")
    launching, release, checking, finished = Event(), Event(), Event(), Event()
    original_launch = workers.launch

    def launch(value):
        launching.set()
        assert release.wait(3)
        return original_launch(value)

    def reconcile():
        checking.set()
        result = controller.reconcile(ident)
        finished.set()
        return result

    workers.launch = launch
    with ThreadPoolExecutor(max_workers=2) as pool:
        resumed = pool.submit(controller.resume, ident)
        assert launching.wait(3)
        reconciled = pool.submit(reconcile)
        assert checking.wait(3)
        try:
            assert not finished.wait(.1)
        finally:
            release.set()
        assert resumed.result(timeout=3)["status"] == "queued"
        assert reconciled.result(timeout=3)["status"] == "queued"
    assert workers.launches == [ident]


def test_timer_reconciles_without_console_and_isolates_probe_failure(control, monkeypatch):
    from chatcopilot.harness import delivery_runtime
    controller, workers, evaluator = control
    failed, stopped = task(controller), task(controller)

    def observe(value):
        if value["task_id"] == failed:
            raise OSError("fixture bus unavailable")
        return WorkerState.INACTIVE

    workers.observe = observe
    monkeypatch.setattr(delivery_runtime, "configuration", lambda: {})
    monkeypatch.setattr(delivery_runtime, "SystemdWorkerControl", lambda *args: workers)
    monkeypatch.setattr("chatcopilot.harness.evaluation_adapter.ServiceEvaluator", lambda **kwargs: evaluator)
    monkeypatch.setattr(delivery_runtime, "pending", lambda store: [])
    assert delivery_runtime.main(["--root", str(controller.store.root),
        "--repository-root", str(controller.repository)]) == 0
    assert controller.store.get(failed)["status"] == "running"
    assert controller.store.get(stopped)["status"] == "interrupted"


@pytest.mark.parametrize("command", ["reconcile", "maintenance"])
def test_cli_uses_public_controls(command, monkeypatch, capsys):
    from chatcopilot.harness import __main__ as cli
    controller = SimpleNamespace(reconcile=Mock(return_value={"status": "interrupted"}), maintenance=Mock(return_value=7))
    monkeypatch.setattr(cli, "HarnessController", lambda *args, **kwargs: controller)
    if command == "reconcile":
        assert cli.main([command, "repair-fixture"]) == 0
        controller.reconcile.assert_called_once_with("repair-fixture")
        assert "interrupted" in capsys.readouterr().out
    else:
        assert cli.main([command, "--", "fixture-command"]) == 7
        controller.maintenance.assert_called_once_with(["fixture-command"])


@pytest.mark.parametrize("output,code,expected", [
    ("LoadState=loaded\nActiveState=active\nJob=\n", 0, WorkerState.ACTIVE),
    ("LoadState=loaded\nActiveState=inactive\nJob=9 /job/9\n", 0, WorkerState.ACTIVE),
    ("LoadState=not-found\nActiveState=inactive\nJob=\n", 4, WorkerState.INACTIVE),
    ("LoadState=loaded\nActiveState=failed\nJob=\n", 0, WorkerState.INACTIVE),
    ("", 1, WorkerState.UNKNOWN),
    ("ActiveState=inactive\n", 0, WorkerState.UNKNOWN),
])
def test_systemd_observation_requires_complete_state_and_no_pending_job(monkeypatch, output, code, expected):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output, returncode=code))
    assert SystemdWorkerControl._unit_state("fixture.service") == expected


def test_worker_execution_lock_prevents_false_inactive(control, monkeypatch):
    controller, _, _ = control
    ident = task(controller)
    directory = controller.store.root / "jobs" / ident
    directory.mkdir(mode=0o700, parents=True)
    fd = os.open(directory / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime = SystemdWorkerControl(controller.repository, controller.store.root, {})
        monkeypatch.setattr(runtime, "_unit_state", lambda unit: WorkerState.INACTIVE)
        assert runtime.observe(controller.store.get(ident)) == WorkerState.ACTIVE
    finally:
        os.close(fd)
    assert runtime.observe(controller.store.get(ident)) == WorkerState.INACTIVE


def test_dispatch_timeout_is_unknown_and_does_not_overwrite_frozen_runtime(control, monkeypatch):
    controller, _, _ = control
    ident = task(controller)
    directory = controller.store.root / "jobs" / ident / "runtime" / "src" / "chatcopilot" / "harness"
    directory.mkdir(mode=0o700, parents=True)
    marker = directory / "worker.py"
    marker.write_text("frozen worker")
    runtime = SystemdWorkerControl(controller.repository, controller.store.root, {})
    monkeypatch.setattr("chatcopilot.harness.worker_runtime.shutil.which", lambda _: "/fixture/systemd-run")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("fixture", 30)))
    assert runtime.launch(controller.store.get(ident)).state == "unknown"
    assert marker.read_text() == "frozen worker"
