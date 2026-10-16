"""Explicit purge must prove ownership and inactivity before removing old tasks."""
from chatcopilot.evals.application.worker_runtime import LocalEvaluationWorker
from types import SimpleNamespace
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from chatcopilot.evals.application import EvaluationApplication
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


def test_cutover_exports_history_and_preserves_old_artifacts_after_maintenance(old_store, tmp_path):
    store, ident = old_store
    workers, evaluator = clients()
    application = EvaluationApplication(tmp_path / "evaluations", worker=LocalEvaluationWorker())
    evaluator.client = Mock(wraps=application)
    before = store.get(ident)
    assert cutover(store, workers, evaluator)["applied"] is False
    assert store.get(ident) == before
    result = cutover(store, workers, evaluator, apply=True)
    assert result["applied"] is True
    assert store.history() == []
    assert (store.root / "jobs" / ident).exists()
    from pathlib import Path
    from chatcopilot.harness.history_repository import verify_history
    history = verify_history(Path(result["history_export"]["path"]))
    assert ident in history["task_ids"]
    assert Path(result["history_export"]["retired_database"]).is_file()
    assert not (store.root / "archives").exists()
    evaluator.client.enter_maintenance.assert_called_once()
    lease = evaluator.client.enter_maintenance.call_args.args[0]
    evaluator.client.leave_maintenance.assert_called_once_with(lease)
    assert application.maintenance_status() is None


def test_cutover_checks_orphan_directories_and_preserves_other_roots(old_store, tmp_path):
    store, _ = old_store
    orphan = store.root / "jobs" / ("repair-" + "b" * 32)
    orphan.mkdir(mode=0o700)
    unrelated = tmp_path / "evaluation-data"
    unrelated.mkdir()
    marker = unrelated / "keep"
    marker.write_text("original evidence")
    workers, evaluator = clients()
    workers.observe = Mock(return_value=WorkerState.INACTIVE)
    result = cutover(store, workers, evaluator, apply=True)
    assert result["task_directories"] == 2
    assert workers.observe.call_count == 4
    assert orphan.exists() and marker.read_text() == "original evidence"


def test_cutover_rejects_symlink_task_directory(old_store, tmp_path):
    store, _ = old_store
    other = tmp_path / "unrelated"
    other.mkdir()
    (store.root / "jobs" / ("repair-" + "b" * 32)).symlink_to(other)
    workers, evaluator = clients()
    with pytest.raises(HarnessError, match="归属"):
        cutover(store, workers, evaluator, apply=True)
    assert other.exists()


def test_cutover_preserves_task_directory_reused_by_a_human_branch(tmp_path):
    from test_harness_repair_v2 import repair
    from chatcopilot.core.source_snapshot import git_output
    from chatcopilot.harness.workspace import prepare
    store, ident, repo = repair.__wrapped__(tmp_path)
    task = store.get(ident)
    worktree = prepare(repo, store.root, ident, task["base_commit"])
    git_output(worktree, "switch", "-c", "human-work")
    store.update(ident, pipeline_version=9, status="blocked")
    workers, evaluator = clients()
    with pytest.raises(HarnessError, match="分支归属"):
        cutover(store, workers, evaluator, repository=repo, apply=True)
    assert worktree.is_dir()
    assert git_output(worktree, "branch", "--show-current") == "human-work"


def test_cutover_preserves_registered_task_worktree_and_branch_in_history(tmp_path):
    from test_harness_repair_v2 import repair
    from chatcopilot.core.source_snapshot import git_output
    from chatcopilot.harness.workspace import prepare
    store, ident, repo = repair.__wrapped__(tmp_path)
    worktree = prepare(repo, store.root, ident, store.get(ident)["base_commit"])
    store.update(ident, pipeline_version=9, status="blocked")
    workers, evaluator = clients()
    result = cutover(store, workers, evaluator, repository=repo, apply=True)
    assert result["applied"] and result["worktrees"] == [str(worktree)]
    assert worktree.exists()
    assert git_output(repo, "branch", "--list", "feat/harness-" + ident[7:])
    assert (repo / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 0\n"


@pytest.mark.parametrize("state", [WorkerState.ACTIVE, WorkerState.UNKNOWN])
def test_unknown_or_active_worker_prevents_cutover(old_store, state):
    store, ident = old_store
    workers, evaluator = clients(state)
    with pytest.raises(HarnessError, match="旧 worker"):
        cutover(store, workers, evaluator, apply=True)
    assert store.get(ident)["status"] == "blocked"
    evaluator.client.enter_maintenance.assert_not_called()


def test_old_queued_delivery_unit_prevents_cutover(old_store):
    store, ident = old_store
    workers, evaluator = clients()
    workers.observe = lambda task: (WorkerState.ACTIVE if task["unit"].startswith("agentstrata-harness-delivery-")
                                     else WorkerState.INACTIVE)
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


def test_closed_pr_auto_merge_metadata_does_not_block_cutover(old_store):
    store, ident = old_store
    store.update(ident, delivery={"pr_number": 4})
    workers, evaluator = clients()
    github = SimpleNamespace(pull=lambda _: {"state": "closed", "auto_merge": {"merge_method": "squash"}},
                             verify_pr=lambda *args: None)
    assert cutover(store, workers, evaluator, apply=True, github=github)["applied"]


def test_old_protocol_blocks_admission_until_explicit_cutover(old_store):
    store, ident = old_store
    with pytest.raises(HarnessError) as caught:
        store.create({**store.get(ident), "task_id": "new", "request_key": "new", "pipeline_version": 11})
    assert caught.value.code == "cutover_required"


def test_export_retains_archives_checkpoints_and_original_credentials(old_store):
    from chatcopilot.harness.history_repository import verify_history
    from chatcopilot.harness.models import PIPELINE_VERSION
    store, ident = old_store
    archive = store.root / "archives" / "old" / "evidence.json"
    archive.parent.mkdir(parents=True, mode=0o700)
    archive.write_text("original archive evidence")
    graphs = store.root / "graphs"
    graphs.mkdir(mode=0o700)
    checkpoint = graphs / "retired.sqlite3"
    with sqlite3.connect(checkpoint) as connection:
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('checkpoint evidence')")
    checkpoint.chmod(0o600)
    credentials = store.root / "jobs" / ident / "sessions" / "plan" / "auth.json"
    credentials.parent.mkdir(parents=True, mode=0o700)
    credentials.write_text("unit-test-credential-placeholder")
    workers, evaluator = clients()
    result = cutover(store, workers, evaluator, apply=True)
    export = Path(result["history_export"]["path"])
    manifest = verify_history(export)
    assert archive.read_text() == "original archive evidence"
    assert checkpoint.exists() and credentials.exists()
    assert (export / "archives" / "old" / "evidence.json").read_text() == archive.read_text()
    assert (export / "graphs" / "retired.sqlite3").exists()
    assert "jobs/" + ident + "/sessions" in manifest["omitted_execution_material"]
    assert not (export / "jobs" / ident / "sessions").exists()
    assert store.protocol_version() == PIPELINE_VERSION and store.history() == []
    with pytest.raises(HarnessError, match="新协议"):
        cutover(store, workers, evaluator, apply=True)


def test_failed_export_never_replaces_active_database(old_store, monkeypatch):
    store, ident = old_store
    before = store.database.path.stat().st_ino
    original = store.get(ident)
    workers, evaluator = clients()

    def invalid(_path):
        raise HarnessError("history_export_invalid", "controlled export failure")

    monkeypatch.setattr("chatcopilot.harness.history_repository.verify_history", invalid)
    with pytest.raises(HarnessError, match="controlled export"):
        cutover(store, workers, evaluator, apply=True)
    assert store.database.path.stat().st_ino == before
    assert store.get(ident) == original
    assert store.protocol_version() is None


def test_database_activation_failure_restores_the_retired_database(old_store, monkeypatch):
    store, ident = old_store
    original = store.get(ident)
    inode = store.database.path.stat().st_ino
    workers, evaluator = clients()
    replace = Path.replace

    def fail_activation(path, target):
        if path.name.startswith(".next-harness-"):
            raise OSError("controlled activation failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_activation)
    with pytest.raises(OSError, match="activation failure"):
        cutover(store, workers, evaluator, apply=True)
    assert store.database.path.stat().st_ino == inode
    assert store.get(ident) == original
