"""Archive/reset must never abandon active execution or destroy recovery data."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.cutover_runtime import cutover
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.store import HarnessStore


@pytest.fixture
def old_store(tmp_path):
    store = HarnessStore(tmp_path / "private")
    store.create({"task_id": "repair-" + "a" * 32, "pipeline_version": 8, "request_key": "old",
        "request_digest": "old", "context_key": "old", "match_key": "old", "active_key": "old",
        "source": {"kind": "robot_task", "original_input": "retained"}})
    ident = store.history()[0]["task_id"]
    store.update(ident, status="blocked")
    with store.database.connect(write=True) as connection:
        connection.execute("DROP TABLE repair_steps")
    return store, ident


def clients(state=WorkerState.INACTIVE):
    return SimpleNamespace(observe=lambda task: state), SimpleNamespace(client=Mock(), execution_status=lambda ident: "completed")


def test_archive_reset_retains_original_artifacts_and_consistent_database(old_store):
    store, ident = old_store
    workers, evaluator = clients()
    before = store.get(ident)
    assert cutover(store, workers, evaluator)["applied"] is False
    assert store.get(ident) == before
    result = cutover(store, workers, evaluator, apply=True)
    assert result["applied"] is True
    assert store.history() == []
    from pathlib import Path
    archive = Path(result["archive"])
    with sqlite3.connect(archive / "harness.sqlite3") as connection:
        archived = json.loads(connection.execute("SELECT payload FROM tasks").fetchone()[0])
    assert archived["task_id"] == ident
    assert (store.root / "jobs" / ident / before["artifact_fields"]["source"]["path"]).is_file()
    evaluator.client.enter_maintenance.assert_called_once()
    evaluator.client.leave_maintenance.assert_called_once()


@pytest.mark.parametrize("state", [WorkerState.ACTIVE, WorkerState.UNKNOWN])
def test_unknown_or_active_worker_prevents_cutover(old_store, state):
    store, ident = old_store
    workers, evaluator = clients(state)
    with pytest.raises(HarnessError, match="旧 worker"):
        cutover(store, workers, evaluator, apply=True)
    assert store.get(ident)["status"] == "blocked"
    evaluator.client.enter_maintenance.assert_not_called()


def test_unfinished_pr_is_not_silently_closed_or_reset(old_store):
    store, ident = old_store
    store.update(ident, delivery={"pr_number": 4})
    workers, evaluator = clients()
    github = SimpleNamespace(pull=lambda _: {"state": "open", "auto_merge": None}, verify_pr=lambda *args: None)
    with pytest.raises(HarnessError, match="PR 尚未"):
        cutover(store, workers, evaluator, apply=True, github=github)
    assert store.get(ident)["delivery"]["pr_number"] == 4


def test_old_protocol_blocks_admission_until_explicit_cutover(old_store):
    store, ident = old_store
    with pytest.raises(HarnessError) as caught:
        store.create({**store.get(ident), "task_id": "new", "request_key": "new", "pipeline_version": 9})
    assert caught.value.code == "cutover_required"
