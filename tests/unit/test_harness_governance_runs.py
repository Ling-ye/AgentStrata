"""Batch policy with controlled workers and GitHub receipts, not live delivery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.harness.control_service import HarnessLifecycle
from chatcopilot.harness.control_types import DispatchResult, WorkerState
from chatcopilot.harness.governance_run_service import GovernanceRuns
from chatcopilot.harness.governance_types import GovernanceOptions
from chatcopilot.harness.models import Cancelled, HarnessError
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.task_budget import TaskBudget


class Workers:
    def __init__(self, store):
        self.store, self.states, self.launches = store, {}, []

    def observe(self, task):
        return self.states.get(task["task_id"], WorkerState.INACTIVE)

    def launch(self, task):
        self.launches.append(task["task_id"])
        self.states[task["task_id"]] = WorkerState.ACTIVE
        return DispatchResult("scheduled")

    def launch_delivery(self, task):
        task = self.store.get(task["task_id"])
        if task.get("delivery_request") == "cancel":
            self.store.update(task["task_id"], delivery={**task["delivery"], "state": "cancelled"})
        return DispatchResult("scheduled")


class Tasks:
    def __init__(self, store, lifecycle):
        self.store, self.lifecycle = store, lifecycle
        self.main, self.started, self.lose_response = "initial-main", [], False

    def start(self, run, sequence, options):
        request = f"{run['run_id']}-{sequence}"
        task, created = self.store.create({"task_id": "repair-" + request, "pipeline_version": 10,
            "request_key": request, "request_digest": request, "context_key": "repository", "match_key": request,
            "active_key": request, "source": {"kind": "code_health"}, "options": asdict(options),
            "repository": run["repository"], "base_commit": self.main, "unit": request,
            "governance_run_id": run["run_id"], "governance_sequence": sequence, "dispatch_state": "creating",
            "delivery": {"state": "pending"}})
        if created:
            self.started.append(task["task_id"])
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError("response lost after durable child creation")
        return task

    def start_learning(self, run, sequence, options, source):
        task = self.start(run, sequence, options)
        return self.store.update(task["task_id"], source={"kind": "code_health", "skill_learning": source},
                                 skill_learning_origin=source["origin_task_id"])

    def launch(self, task_id):
        with self.lifecycle.operation(task_id):
            self.lifecycle.launch_locked(task_id)

    def resume(self, task_id):
        with self.lifecycle.operation(task_id):
            self.lifecycle.prepare_resume_locked(task_id)
            self.lifecycle.claim_resume_locked(task_id)
            self.lifecycle.launch_locked(task_id)

    def retry_delivery(self, task_id):
        self.store.update(task_id, delivery_cancel_requested=False,
            delivery={**self.store.get(task_id)["delivery"], "state": "pending"})


@pytest.fixture
def batch(tmp_path):
    store = HarnessStore(tmp_path / "private")
    workers = Workers(store)
    lifecycle = HarnessLifecycle(store, workers, Mock())
    port = Tasks(store, lifecycle)
    service = GovernanceRuns(store, lifecycle, port, str(tmp_path / "repo"))
    return SimpleNamespace(store=store, workers=workers, lifecycle=lifecycle, port=port, service=service)


def count_options(count=2):
    return GovernanceOptions("fixture", stop_condition={"mode": "findings", "count": count})


def finish(batch, run, *, status="fixed", delivery="merged", found=True, elapsed=10):
    ident = run["current_task_id"]
    batch.workers.states[ident] = WorkerState.INACTIVE
    return batch.store.update(ident, status=status, stage="done", elapsed_seconds=elapsed,
        governance_finding_id="finding-" + ident if found else None,
        accepted_candidate={"attempt": 1} if status == "fixed" else None,
        delivery={"state": delivery, "pr_number": 1, "pr_url": "https://example.invalid/pr/1"} if status == "fixed" else {"state": "no_changes"})


def test_two_findings_wait_for_merge_and_stopped_worker_then_freeze_new_main(batch):
    run = batch.service.start(count_options(), request_id="start")
    first = run["current_task_id"]
    assert len(batch.port.started) == 1
    assert batch.store.get(first)["options"]["timeout_seconds"] is None
    finish(batch, run, delivery="waiting_checks")
    waiting = batch.service.advance(run["run_id"])
    assert waiting["status"] == "waiting_delivery" and waiting["found_count"] == 1
    assert waiting["merged_count"] == 0 and len(batch.port.started) == 1
    finish(batch, run)
    batch.workers.states[first] = WorkerState.ACTIVE
    batch.service.advance(run["run_id"])
    assert len(batch.port.started) == 1
    batch.port.main = "main-with-first-fix"
    batch.workers.states[first] = WorkerState.INACTIVE
    second = batch.service.advance(run["run_id"])
    assert second["sequence"] == 2 and second["current_task_id"] != first
    assert second["tasks"][1]["base_commit"] == "main-with-first-fix"
    batch.service.advance(run["run_id"])
    assert len(batch.port.started) == 2
    finish(batch, second)
    done = batch.service.advance(run["run_id"])
    assert done["status"] == "completed" and done["stop_reason"] == "findings_limit"
    assert done["found_count"] == done["merged_count"] == 2
    assert done["elapsed_seconds"] == 20
    batch.service.advance(run["run_id"])
    assert len(batch.port.started) == 2


def test_nth_finding_still_finishes_repair_and_delivery(batch):
    run = batch.service.start(count_options(1))
    finish(batch, run, delivery="pr_open")
    waiting = batch.service.advance(run["run_id"])
    assert waiting["found_count"] == 1 and waiting["status"] == "waiting_delivery"
    finish(batch, run)
    assert batch.service.advance(run["run_id"])["status"] == "completed"
    assert len(batch.port.started) == 1


def test_no_findings_finishes_without_repeated_scanning(batch):
    run = batch.service.start(count_options(9))
    finish(batch, run, status="no_changes", found=False)
    done = batch.service.advance(run["run_id"])
    assert done["status"] == "completed" and done["stop_reason"] == "no_changes"
    assert done["found_count"] == done["merged_count"] == 0
    assert len(batch.port.started) == 1


@pytest.mark.parametrize("status,delivery", [("needs_review", "no_changes"), ("failed", "no_changes"),
    ("blocked", "no_changes"), ("fixed", "blocked"), ("fixed", "checks_failed"), ("fixed", "closed")])
def test_failure_or_sensitive_item_stops_whole_run(batch, status, delivery):
    run = batch.service.start(count_options())
    finish(batch, run, status=status, delivery=delivery)
    stopped = batch.service.advance(run["run_id"])
    assert stopped["status"] == "blocked" and stopped["found_count"] == 1
    batch.service.advance(run["run_id"])
    assert len(batch.port.started) == 1


def test_duplicate_requests_and_concurrent_callbacks_do_not_duplicate_children(batch):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: batch.service.start(count_options(), request_id="same"), range(2)))
    assert results[0]["run_id"] == results[1]["run_id"]
    assert len(batch.port.started) == len(batch.workers.launches) == 1
    with pytest.raises(HarnessError, match="内容已变化"):
        batch.service.start(count_options(3), request_id="same")
    with pytest.raises(HarnessError, match="活动"):
        batch.service.start(count_options(), request_id="other")


def test_lost_creation_receipt_and_service_restart_recover_same_child(batch):
    batch.port.lose_response = True
    run = batch.service.start(count_options(), request_id="lost")
    assert run["status"] == "blocked" and len(batch.port.started) == 1
    service = GovernanceRuns(batch.store, batch.lifecycle, batch.port, batch.service.repository)
    resumed = service.resume(run["run_id"])
    assert resumed["current_task_id"] == run["current_task_id"]
    assert len(batch.port.started) == len(batch.workers.launches) == 1
    assert service.start(count_options(), request_id="lost")["run_id"] == run["run_id"]


def test_cancel_intent_precedes_child_cancellation_and_waits_for_actual_stop(batch):
    run = batch.service.start(count_options())
    cancelling = batch.service.cancel(run["run_id"])
    assert cancelling["status"] == "cancel_requested"
    assert batch.store.get(run["current_task_id"])["status"] == "cancel_requested"
    batch.workers.states[run["current_task_id"]] = WorkerState.UNKNOWN
    assert batch.service.advance(run["run_id"])["status"] == "cancel_requested"
    batch.workers.states[run["current_task_id"]] = WorkerState.INACTIVE
    assert batch.service.advance(run["run_id"])["status"] == "cancelled"
    assert len(batch.port.started) == 1


def test_cancel_between_merge_and_next_discovery_never_creates_next_item(batch):
    run = batch.service.start(count_options())
    finish(batch, run)
    assert batch.service.cancel(run["run_id"])["status"] == "cancelled"
    assert len(batch.port.started) == 1


def test_time_budget_is_shared_and_waiting_does_not_consume_it(batch):
    run = batch.service.start(GovernanceOptions("fixture", stop_condition={"mode": "time", "seconds": 30}))
    finish(batch, run, delivery="waiting_checks", elapsed=12)
    for _ in range(3):
        assert batch.service.advance(run["run_id"])["elapsed_seconds"] == 12
    finish(batch, run, elapsed=12)
    second = batch.service.advance(run["run_id"])
    assert batch.store.get(second["current_task_id"])["options"]["timeout_seconds"] == 18
    finish(batch, second, elapsed=18)
    done = batch.service.advance(run["run_id"])
    assert done["status"] == "completed" and done["stop_reason"] == "budget_exhausted"
    assert done["elapsed_seconds"] == 30 and len(batch.port.started) == 2


def test_task_budget_accounts_active_segments_and_resume_keeps_usage(batch):
    run = batch.service.start(GovernanceOptions("fixture", stop_condition={"mode": "time", "seconds": 10}))
    ident = run["current_task_id"]
    clock = [0.0]
    with TaskBudget(batch.store, ident, clock=lambda: clock[0]) as budget:
        clock[0] = 4
        budget.check()
    assert batch.store.get(ident)["elapsed_seconds"] == 4
    clock[0] = 10000  # No execution segment during the wait.
    with pytest.raises(HarnessError, match="预算已用完"):
        with TaskBudget(batch.store, ident, clock=lambda: clock[0]) as budget:
            assert budget.remaining == 6
            clock[0] += 6
            budget.check()
    assert batch.store.get(ident)["elapsed_seconds"] == 10
    finish(batch, run, status="blocked", elapsed=10)
    batch.service.advance(run["run_id"])
    with pytest.raises(HarnessError, match="不会重置"):
        batch.service.resume(run["run_id"])


def test_count_mode_has_no_hidden_deadline_and_cancel_still_works(batch):
    run = batch.service.start(count_options(1))
    ident = run["current_task_id"]
    clock = [0]
    with TaskBudget(batch.store, ident, clock=lambda: clock[0]) as budget:
        clock[0] = 3600 * 24
        budget.check()
        assert budget.remaining is None
        batch.service.cancel(run["run_id"])
        with pytest.raises(Cancelled):
            budget.check()
    assert batch.store.get(ident)["remaining_seconds"] is None


def test_read_only_projection_counts_a_finding_once_across_attempts(batch):
    run = batch.service.start(count_options())
    ident = run["current_task_id"]
    batch.store.update(ident, governance_finding_id="same", current_attempt=3)
    before = batch.service.runs.get(run["run_id"])
    assert batch.service.get(run["run_id"])["found_count"] == 1
    assert batch.service.get(run["run_id"])["found_count"] == 1
    assert batch.service.runs.get(run["run_id"]) == before


def test_additive_run_schema_preserves_existing_tasks_and_shared_database_version(batch):
    run = batch.service.start(count_options())
    original = batch.store.get(run["current_task_id"])
    GovernanceRuns(batch.store, batch.lifecycle, batch.port, batch.service.repository)
    assert batch.store.get(run["current_task_id"]) == original
    with batch.store.database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize("stop", [{"mode": "findings", "count": 0}, {"mode": "findings", "count": True},
    {"mode": "time", "seconds": 1, "count": 2}, {"mode": "time", "seconds": None}, {"mode": "unknown"}])
def test_invalid_stopping_conditions_fail_at_domain_boundary(stop):
    with pytest.raises(ValueError):
        GovernanceOptions("fixture", stop_condition=stop)


def test_controller_run_entrypoint_creates_and_freezes_ordinary_children(tmp_path, monkeypatch):
    import subprocess
    from chatcopilot.harness.api import HarnessController
    from chatcopilot.harness import delivery
    from chatcopilot.core.source_snapshot import git_output
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    rules = repo / "docs/reference/harness-principles.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("# Frozen principles\nPreserve behavior and remove unused code.\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    first_sha = git("rev-parse", "HEAD")
    def baseline(repository, _settings):
        return {"version": 1, "state": "pending", "repository": "fixture/repo", "actor": "fixture",
                "base_branch": "main", "base_sha": git_output(repository, "rev-parse", "HEAD"),
                "author_name": "Fixture", "author_email": "fixture@example.invalid", "auto_merge": False}
    class LocalSnapshot:
        def verify_target(self, _state):
            pass
        def fetch(self, repository, _directory, sha):
            git_output(repository, "cat-file", "-e", sha + "^{commit}")
    original = delivery.initialize
    monkeypatch.setattr(delivery, "remote_baseline", baseline)
    monkeypatch.setattr(delivery, "initialize", lambda store, ident: original(store, ident, client=LocalSnapshot()))
    workers = Workers(None)
    controller = HarnessController(repo, root=tmp_path / "private", worker_control=workers, evaluator=Mock())
    workers.store = controller.store
    run = controller.start_code_health(count_options(), request_id="controller")
    assert run["status"] == "running", run
    first = controller.store.get(run["current_task_id"])
    assert first["base_commit"] == first_sha and first["options"]["timeout_seconds"] is None
    assert first["principles"] and first["governance_run_id"] == run["run_id"]
    controller.store.update(first["task_id"], status="fixed", governance_finding_id="first", delivery={"state": "merged"})
    workers.states[first["task_id"]] = WorkerState.INACTIVE
    (repo / "next.txt").write_text("Merged first fix\n")
    git("add", "next.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "merged first fix")
    second = controller._governance_runs().advance(run["run_id"])
    current = controller.store.get(second["current_task_id"])
    assert current["base_commit"] == git("rev-parse", "HEAD") != first_sha
    frozen = controller.store.root / "jobs" / current["task_id"] / "source"
    assert (frozen / "next.txt").read_text() == "Merged first fix\n"
    with pytest.raises(HarnessError, match="批次恢复"):
        controller.resume(current["task_id"])
    with pytest.raises(HarnessError, match="批次恢复"):
        controller.retry_delivery(current["task_id"])


def test_resume_preserves_finding_attempts_and_elapsed_time(batch):
    run = batch.service.start(GovernanceOptions("fixture", stop_condition={"mode": "time", "seconds": 30}))
    ident = run["current_task_id"]
    finish(batch, run, status="blocked", elapsed=7)
    batch.store.save_attempt(ident, 1, {"number": 1, "status": "rejected", "counts_toward_budget": True})
    batch.service.advance(run["run_id"])
    resumed = batch.service.resume(run["run_id"])
    assert resumed["found_count"] == 1 and resumed["elapsed_seconds"] == 7
    assert resumed["current_task_id"] == ident and len(batch.port.started) == 1
    assert len(batch.store.attempts(ident)) == 1
    assert batch.store.get(ident)["options"]["max_attempts"] == 3


def test_run_from_another_repository_cannot_be_resumed_or_cancelled(batch):
    run = batch.service.start(count_options())
    foreign = GovernanceRuns(batch.store, batch.lifecycle, batch.port, "/other/repository")
    for action in (foreign.get, foreign.advance, foreign.cancel, foreign.resume):
        with pytest.raises(HarnessError, match="此仓库"):
            action(run["run_id"])
    assert batch.service.get(run["run_id"])["status"] == "running"


def test_old_gc_worker_and_delivery_are_not_relaunched(batch):
    from chatcopilot.harness.api import HarnessController
    from chatcopilot.harness.delivery_runtime import pending
    from chatcopilot.harness.worker_runtime import worker_execution
    run = batch.service.start(count_options())
    ident = run["current_task_id"]
    old = batch.store.update(ident, governance_run_id=None, status="fixed", accepted_candidate={"attempt": 1})
    assert HarnessController._public(old)["archived"] is True
    assert ident not in pending(batch.store)
    with pytest.raises(HarnessError, match="旧任务只读"):
        with worker_execution(batch.store, ident, delivery=True):
            pytest.fail("old GC must not execute in the new workflow")


def test_lifecycle_tick_between_child_creation_and_launch_cannot_interrupt_batch(batch, monkeypatch):
    original = batch.port.start
    def prepare_then_reconcile(*args, **kwargs):
        child = original(*args, **kwargs)
        batch.lifecycle.reconcile(child["task_id"])
        return batch.store.get(child["task_id"])
    monkeypatch.setattr(batch.port, "start", prepare_then_reconcile)
    run = batch.service.start(count_options(1))
    child = batch.store.get(run["current_task_id"])
    assert child["status"] == "queued"
    assert child["dispatch_state"] == "scheduled"
    assert batch.workers.launches == [child["task_id"]]
    assert run["status"] == "running"



def test_merged_harness_improvement_dispatches_one_learning_task_then_no_change(batch):
    run = batch.service.start(count_options(1))
    origin = run["current_task_id"]
    target = {"summary": "Remove duplicated check", "impact": "duplicate work",
        "principle_refs": ["docs/reference/harness.md"],
        "affected_paths": ["src/chatcopilot/harness/worker.py"],
        "evidence": [{"path": "src/chatcopilot/harness/worker.py", "start_line": 1, "end_line": 1}]}
    batch.store.update(origin, source={"kind": "code_health", "governance_target": target})
    batch.store.save_attempt(origin, 1, {"number": 1, "status": "accepted",
        "changed_files": ["src/chatcopilot/harness/worker.py"],
        "review": {"decision": "approved", "improvements": []}})
    finish(batch, run)
    learning_run = batch.service.advance(run["run_id"])
    child = learning_run["current_task_id"]
    assert child != origin and learning_run["found_count"] == learning_run["merged_count"] == 1
    assert batch.store.get(child)["source"]["skill_learning"]["origin_task_id"] == origin
    batch.service.advance(run["run_id"])
    assert len(batch.port.started) == 2
    finish(batch, learning_run, status="no_changes", found=False)
    done = batch.service.advance(run["run_id"])
    assert done["status"] == "completed" and done["found_count"] == done["merged_count"] == 1
    assert batch.store.get(origin)["skill_learning"] == {"state": "no_change", "task_id": child}
    batch.service.advance(run["run_id"])
    assert len(batch.port.started) == 2



def test_merged_skill_pr_is_excluded_from_finding_count_and_next_issue_uses_new_main(batch):
    run = batch.service.start(count_options(2))
    origin = run["current_task_id"]
    target = {"summary": "Remove duplicated check", "impact": "duplicate work",
        "principle_refs": ["docs/reference/harness.md"],
        "affected_paths": ["src/chatcopilot/harness/worker.py"],
        "evidence": [{"path": "src/chatcopilot/harness/worker.py", "start_line": 1, "end_line": 1}]}
    batch.store.update(origin, source={"kind": "code_health", "governance_target": target})
    batch.store.save_attempt(origin, 1, {"number": 1, "status": "accepted",
        "changed_files": ["src/chatcopilot/harness/worker.py"],
        "review": {"decision": "approved", "improvements": []}})
    finish(batch, run)
    learning_run = batch.service.advance(run["run_id"])
    child = learning_run["current_task_id"]
    finish(batch, learning_run)
    batch.port.main = "main-with-new-skill"
    next_run = batch.service.advance(run["run_id"])
    assert next_run["current_task_id"] not in {origin, child}, next_run
    assert next_run["found_count"] == next_run["merged_count"] == 1
    assert batch.store.get(next_run["current_task_id"])["base_commit"] == "main-with-new-skill"
    assert batch.store.get(origin)["skill_learning"] == {"state": "merged", "task_id": child}
    assert next_run["tasks"][1]["purpose"] == "skill_learning"
    assert next_run["tasks"][2]["purpose"] == "code_health"
