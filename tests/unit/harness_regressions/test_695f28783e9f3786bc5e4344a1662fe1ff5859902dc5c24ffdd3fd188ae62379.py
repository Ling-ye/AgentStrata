"""Regression for the selected governance-run active-status consolidation."""

from __future__ import annotations

import ast
from dataclasses import asdict
import inspect
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

from chatcopilot.harness import governance_run_repository, governance_run_service, governance_types
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.governance_run_repository import GovernanceRunRepository
from chatcopilot.harness.governance_run_service import GovernanceRuns
from chatcopilot.harness.governance_runtime import reconcile_runs
from chatcopilot.harness.governance_types import GovernanceOptions
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.store import HarnessStore


ACTIVE = {"running", "waiting_delivery", "cancel_requested"}
TERMINAL = {"completed", "blocked", "cancelled"}
OLD_INDEX = ("CREATE UNIQUE INDEX governance_run_active ON governance_runs(repository) "
             "WHERE status IN ('running','waiting_delivery','cancel_requested')")


def _index_sql(store):
    with store.database.connect() as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='governance_run_active'"
        ).fetchone()
    assert row is not None
    return row[0]


def _normalized_sql(value):
    return re.sub(r"\s+", " ", value).replace(" IF NOT EXISTS", "").strip()


def _old_database(store):
    with store.database.connect(write=True) as connection:
        connection.execute("""CREATE TABLE governance_runs (
            run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            repository TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        connection.execute(OLD_INDEX)
        historical = {"run_id": "gc-historical", "request_id": "historical",
                      "repository": "another/repo", "status": "completed", "options": {}}
        connection.execute(
            "INSERT INTO governance_runs VALUES (?,?,?,?,?,?,?)",
            (historical["run_id"], historical["request_id"], historical["repository"],
             historical["status"], json.dumps(historical), 1.0, 2.0),
        )


def test_active_statuses_have_one_types_source_and_frozen_index(tmp_path):
    assert governance_types.RUN_ACTIVE == ACTIVE
    assert governance_run_service.RUN_ACTIVE is governance_types.RUN_ACTIVE

    # The selected entropy is the duplicate SQL literals in Repo. Checking the
    # source owner is appropriate here; runtime checks below cover the behavior.
    repository_tree = ast.parse(inspect.getsource(governance_run_repository))
    repository_literals = [node.value for node in ast.walk(repository_tree)
                           if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not any("waiting_delivery" in value or "cancel_requested" in value
                   for value in repository_literals)
    active_imports = {
        alias.asname or alias.name
        for node in ast.walk(repository_tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "chatcopilot.harness.governance_types"
        for alias in node.names
        if alias.name.startswith("RUN_ACTIVE")
    }
    assert active_imports
    assert any(isinstance(node, ast.Name) and node.id in active_imports
               for node in ast.walk(repository_tree))

    store = HarnessStore(tmp_path / "new")
    GovernanceRunRepository(store)
    assert _normalized_sql(_index_sql(store)) == OLD_INDEX


@pytest.mark.parametrize("existing", [False, True], ids=["new-db", "existing-db"])
@pytest.mark.parametrize("status", sorted(ACTIVE | TERMINAL))
def test_run_repository_preserves_active_exclusivity_and_payload(tmp_path, existing, status):
    store = HarnessStore(tmp_path / "private")
    if existing:
        _old_database(store)
    index_before = _index_sql(store) if existing else None
    runs = GovernanceRunRepository(store)
    assert _normalized_sql(_index_sql(store)) == OLD_INDEX
    if existing:
        assert _index_sql(store) == index_before
        assert runs.get("gc-historical")["status"] == "completed"
        with store.database.connect() as connection:
            historical_payload = connection.execute(
                "SELECT payload FROM governance_runs WHERE run_id='gc-historical'"
            ).fetchone()[0]
        assert historical_payload == json.dumps({
            "run_id": "gc-historical", "request_id": "historical",
            "repository": "another/repo", "status": "completed", "options": {},
        })

    options = GovernanceOptions("fixture", stop_condition={"mode": "findings", "count": 2}).to_payload()
    first, created = runs.create("fixture/repo", options, "first")
    assert created and first["status"] == "running"
    first = runs.update(first["run_id"], status=status)
    assert runs.get(first["run_id"]) == first
    with store.database.connect() as connection:
        stored_status, stored_payload = connection.execute(
            "SELECT status,payload FROM governance_runs WHERE run_id=?", (first["run_id"],)
        ).fetchone()
    assert stored_status == status and json.loads(stored_payload) == first

    active_here = {run["run_id"] for run in runs.active("fixture/repo")}
    active_all = {run["run_id"] for run in runs.active()}
    assert (first["run_id"] in active_here) is (status in ACTIVE)
    assert active_here <= active_all
    assert runs.active("another/repo") == []

    if status in ACTIVE:
        with pytest.raises(HarnessError) as error:
            runs.create("fixture/repo", options, "second")
        assert error.value.code == "governance_active"
        with pytest.raises(sqlite3.IntegrityError):
            with store.database.connect(write=True) as connection:
                connection.execute(
                    "INSERT INTO governance_runs VALUES (?,?,?,?,?,?,?)",
                    ("gc-duplicate", "raw-second", "fixture/repo", "running", "{}", 3.0, 3.0),
                )
    else:
        second, created = runs.create("fixture/repo", options, "second")
        assert created and second["run_id"] in {run["run_id"] for run in runs.active("fixture/repo")}

    repeated, created = runs.create("fixture/repo", options, "first")
    assert not created and repeated["run_id"] == first["run_id"]
    with pytest.raises(HarnessError) as error:
        runs.create("fixture/repo", {**options, "max_attempts": 2}, "first")
    assert error.value.code == "conflict"


class _Workers:
    def __init__(self):
        self.states = {}

    def observe(self, task):
        return self.states.get(task["task_id"], WorkerState.INACTIVE)


class _Lifecycle:
    def __init__(self, store, workers):
        self.store, self.workers = store, workers
        self.reconciled = []

    def reconcile(self, task_id):
        self.reconciled.append(task_id)

    def cancel(self, task_id):
        task = self.store.get(task_id)
        if self.workers.observe(task) == WorkerState.INACTIVE:
            return self.store.update(task_id, status="cancelled", delivery={"state": "cancelled"})
        return self.store.update(task_id, status="cancel_requested")

    def require_stopped(self, task):
        assert self.workers.observe(task) == WorkerState.INACTIVE


class _Tasks:
    def __init__(self, store, workers):
        self.store, self.workers = store, workers
        self.started = []
        self.preflights = []

    def preflight_model(self, model, reasoning_effort):
        self.preflights.append((model, reasoning_effort))

    def start(self, run, sequence, options):
        request = f"{run['run_id']}-{sequence}"
        task, created = self.store.create({
            "task_id": "repair-" + request,
            "pipeline_version": 10,
            "request_key": request,
            "request_digest": request,
            "match_key": request,
            "context_key": "repository",
            "active_key": request,
            "source": {"kind": "code_health"},
            "options": asdict(options),
            "repository": run["repository"],
            "governance_run_id": run["run_id"],
            "governance_sequence": sequence,
            "dispatch_state": "creating",
            "delivery": {"state": "pending"},
        })
        if created:
            self.started.append(task["task_id"])
        return task

    def launch(self, task_id):
        self.store.update(task_id, dispatch_state="scheduled")
        self.workers.states[task_id] = WorkerState.ACTIVE

    def resume(self, task_id):
        self.store.update(task_id, status="queued", dispatch_state="creating")

    def retry_delivery(self, task_id):
        self.store.update(task_id, delivery={"state": "pending"})


def test_service_creation_resume_cancel_and_reconciliation_poll(tmp_path):
    store = HarnessStore(tmp_path / "private")
    workers = _Workers()
    lifecycle = _Lifecycle(store, workers)
    tasks = _Tasks(store, workers)
    repository = str(tmp_path / "repo")
    service = GovernanceRuns(store, lifecycle, tasks, repository)
    options = GovernanceOptions("fixture", stop_condition={"mode": "findings", "count": 2})

    started = service.start(options, request_id="service-request")
    run_id, task_id = started["run_id"], started["current_task_id"]
    assert started["status"] == "running" and tasks.started == [task_id]
    assert service.start(options, request_id="service-request")["run_id"] == run_id
    assert tasks.started == [task_id]
    with pytest.raises(HarnessError) as error:
        service.start(options, request_id="another-request")
    assert error.value.code == "governance_active"

    workers.states[task_id] = WorkerState.INACTIVE
    store.update(task_id, status="blocked")
    assert service.advance(run_id)["status"] == "blocked"
    resumed = service.resume(run_id)
    assert resumed["status"] == "running" and resumed["current_task_id"] == task_id
    assert tasks.started == [task_id]

    # The runtime tick reassembles GovernanceRuns and discovers the same active
    # batch through Repo.active; only external worker operations are faked.
    controller = SimpleNamespace(store=store, lifecycle=lifecycle, repository=repository)
    before = len(lifecycle.reconciled)
    reconcile_runs(controller)
    assert len(lifecycle.reconciled) == before + 1

    store.update(task_id, status="fixed", delivery={"state": "pending"})
    workers.states[task_id] = WorkerState.INACTIVE
    waiting = service.advance(run_id)
    assert waiting["status"] == "waiting_delivery"
    before = len(lifecycle.reconciled)
    reconcile_runs(controller)
    assert len(lifecycle.reconciled) == before + 1
    assert service.get(run_id)["status"] == "waiting_delivery"

    workers.states[task_id] = WorkerState.ACTIVE
    cancelling = service.cancel(run_id)
    assert cancelling["status"] == "cancel_requested"
    assert service.runs.active(repository)[0]["run_id"] == run_id
    workers.states[task_id] = WorkerState.INACTIVE
    reconcile_runs(controller)
    assert service.get(run_id)["status"] == "cancelled"
    assert service.runs.active(repository) == []
