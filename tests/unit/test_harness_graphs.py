"""Durable graph recovery with isolated SQLite, real worktrees and controlled roles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from contextlib import contextmanager

import pytest
from langgraph.graph import END, START, StateGraph

from chatcopilot.harness.agent_types import Role
from chatcopilot.harness.graph_repository import checkpoints
from chatcopilot.harness.graph_service import execute_graph
from chatcopilot.harness.graph_types import GraphState
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.repair_runtime import run_task
from chatcopilot.harness.role_service import RoleWorkflow
from test_harness_multi_agent import Roles
from test_harness_repair_v2 import Verifier, repair as repair_fixture


class ProcessStopped(BaseException):
    """Bypass business retry handling, as an abrupt worker termination would."""


@pytest.fixture
def repair(tmp_path):
    return repair_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("role", [Role.PLAN, Role.CODING])
def test_confirmed_role_result_survives_loss_of_the_node_checkpoint(repair, monkeypatch, role):
    store, ident, _ = repair
    runner = Roles()
    original = RoleWorkflow.call
    crashed = False

    def stop_after_result(self, current, *args, **kwargs):
        nonlocal crashed
        result = original(self, current, *args, **kwargs)
        if current == role and not crashed:
            crashed = True
            raise ProcessStopped()
        return result

    monkeypatch.setattr(RoleWorkflow, "call", stop_after_result)
    with pytest.raises(ProcessStopped):
        run_task(store, ident, Verifier(), runner)
    assert store.get(ident)["role_executions"][f"{role.value}-1"]["state"] == "complete"
    with checkpoints(store.root, "task:" + ident) as saver:
        snapshot = saver.get_tuple({"configurable": {"thread_id": "task:" + ident}})
        assert snapshot is not None
        assert json.dumps(snapshot.checkpoint["channel_values"])
    store.update(ident, status="queued")
    result = run_task(store, ident, Verifier(), runner)
    assert result["status"] == "fixed", result.get("message")
    assert runner.roles.count(role) == 1
    assert runner.calls == 1 and len(store.attempts(ident)) == 1
    assert result["graph"]["next"] == []


def test_unconfirmed_coding_is_not_replayed_or_accepted(repair):
    store, ident, _ = repair

    class Interrupted(Roles):
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role == Role.CODING:
                raise ProcessStopped()
            return result

    runner = Interrupted()
    with pytest.raises(ProcessStopped):
        run_task(store, ident, Verifier(), runner)
    assert store.get(ident)["role_executions"]["coding-1"]["state"] == "running"
    store.update(ident, status="queued")
    result = run_task(store, ident, Verifier(), runner)
    assert result["error_code"] == "session_unconfirmed"
    assert runner.calls == 1 and not result.get("accepted_candidate")
    assert len(store.attempts(ident)) == 1


def test_repeated_resume_cannot_bypass_an_unknown_role_session(repair):
    from types import SimpleNamespace
    from chatcopilot.harness.control_service import HarnessLifecycle
    from chatcopilot.harness.control_types import WorkerState

    store, ident, _ = repair
    store.update(ident, status="blocked", role_executions={"coding-1": {"state": "uncertain"}})
    lifecycle = HarnessLifecycle(
        store, SimpleNamespace(observe=lambda task: WorkerState.INACTIVE), object()
    )
    for _ in range(2):
        with pytest.raises(HarnessError, match="角色会话终态"):
            lifecycle.prepare_resume_locked(ident)
    assert store.get(ident)["status"] == "blocked"


def test_checkpoint_failure_after_plan_does_not_repeat_the_model(repair, monkeypatch):
    from langgraph.checkpoint.sqlite import SqliteSaver

    store, ident, _ = repair
    runner = Roles()
    original = SqliteSaver.put
    broken = False

    def fail_once(self, config, checkpoint, metadata, new_versions):
        nonlocal broken
        if (
            not broken
            and "plan-1" in store.get(ident).get("role_executions", {})
            and store.get(ident)["role_executions"]["plan-1"]["state"] == "complete"
        ):
            broken = True
            raise sqlite3.OperationalError("controlled checkpoint failure")
        return original(self, config, checkpoint, metadata, new_versions)

    monkeypatch.setattr(SqliteSaver, "put", fail_once)
    first = run_task(store, ident, Verifier(), runner)
    assert first["status"] == "blocked" and first["error_code"] == "storage_error"
    store.update(ident, status="queued")
    result = run_task(store, ident, Verifier(), runner)
    assert result["status"] == "fixed", result.get("message")
    assert runner.roles.count(Role.PLAN) == 1
    assert store.attempts(ident)[0]["counts_toward_budget"]


@pytest.mark.parametrize("phase", ["seal", "close"])
def test_accepted_receipt_survives_checkpoint_commit_or_close_failure(repair, monkeypatch, phase):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from chatcopilot.harness import workflow

    store, ident, _ = repair
    runner = Roles()
    failed = False
    if phase == "seal":
        original = SqliteSaver.put

        def break_commit(self, *args, **kwargs):
            nonlocal failed
            if not failed and store.get(ident)["status"] == "fixed":
                failed = True
                raise sqlite3.OperationalError("controlled accepted-checkpoint failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(SqliteSaver, "put", break_commit)
    else:
        original = workflow.checkpoints

        @contextmanager
        def break_close(*args):
            nonlocal failed
            with original(*args) as saver:
                yield saver
            if not failed:
                failed = True
                raise sqlite3.OperationalError("controlled checkpoint close failure")

        monkeypatch.setattr(workflow, "checkpoints", break_close)
    first = run_task(store, ident, Verifier(), runner)
    assert first["status"] == "blocked" and first.get("accepted_candidate")
    store.claim_resume(ident)
    result = run_task(store, ident, Verifier(), runner)
    assert result["status"] == "fixed", result.get("message")
    assert runner.roles == [Role.PLAN, Role.CODING, Role.TEST, Role.REVIEW]
    assert len(store.attempts(ident)) == 1


def test_cancellation_after_checkpoint_never_starts_the_next_role(repair, monkeypatch):
    store, ident, _ = repair
    runner = Roles()
    original = RoleWorkflow.call

    def stop_after_plan(self, role, *args, **kwargs):
        result = original(self, role, *args, **kwargs)
        if role == Role.PLAN:
            raise ProcessStopped()
        return result

    monkeypatch.setattr(RoleWorkflow, "call", stop_after_plan)
    with pytest.raises(ProcessStopped):
        run_task(store, ident, Verifier(), runner)
    store.update(ident, status="cancel_requested", delivery_cancel_requested=True)
    result = run_task(store, ident, Verifier(), runner)
    assert result["status"] == "cancelled"
    assert runner.calls == 0 and not result.get("accepted_candidate")


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_checkpoint_files_cannot_follow_symlinks(tmp_path, suffix):
    from chatcopilot.core.private_sqlite import private_directory

    root = private_directory(tmp_path / "private")
    directory = private_directory(root / "graphs")
    identity = "task:fixture"
    key = hashlib.sha256(identity.encode()).hexdigest()
    other = tmp_path / "unrelated"
    other.write_bytes(b"original bytes")
    other.chmod(0o600)
    (directory / (key + ".sqlite3" + suffix)).symlink_to(other)
    with pytest.raises(ValueError):
        with checkpoints(root, identity):
            pytest.fail("unsafe checkpoint opened")
    assert other.read_bytes() == b"original bytes"


def test_incompatible_graph_version_stops_before_any_node(tmp_path):
    from chatcopilot.core.private_sqlite import private_directory

    root = private_directory(tmp_path / "private")
    calls = []
    builder = StateGraph(GraphState)
    builder.add_node("work", lambda state: calls.append(True) or {})
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    with checkpoints(root, "task:fixture") as saver:
        graph = builder.compile(checkpointer=saver)
        graph.update_state(
            {"configurable": {"thread_id": "task:fixture"}},
            {"identity": "task:fixture", "version": 999},
        )
        with pytest.raises(HarnessError, match="图版本"):
            execute_graph(graph, "task:fixture", initial={})
    assert calls == []


def test_checkpoint_contains_references_and_plain_state(repair):
    store, ident, _ = repair
    result = run_task(store, ident, Verifier(), Roles())
    assert result["status"] == "fixed"
    with checkpoints(store.root, "task:" + ident) as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": "task:" + ident}})
        values = checkpoint.checkpoint["channel_values"]
        encoded = json.dumps(values)
        assert "role_context" in encoded
        assert "base_commit" not in values and "model_settings" not in values
    database = (
        store.root
        / "graphs"
        / (hashlib.sha256(("task:" + ident).encode()).hexdigest() + ".sqlite3")
    )
    assert database.stat().st_mode & 0o777 == 0o600


def test_completed_graph_cannot_restore_a_mismatched_verified_manifest(repair):
    store, ident, _ = repair
    runner = Roles()
    result = run_task(store, ident, Verifier(), runner)
    assert result["status"] == "fixed" and result["verified_manifest"]
    store.interrupt(ident, storage_error=sqlite3.OperationalError("controlled projection failure"))
    store.update(ident, verified_manifest={})
    store.claim_resume(ident)
    blocked = run_task(store, ident, Verifier(), runner)
    assert blocked["status"] == "blocked" and blocked["error_code"] == "incomplete_verification"
    assert runner.calls == 1


def test_task_graph_hands_off_to_delivery_and_resumes_one_pr(repair, tmp_path, monkeypatch):
    """Real Git/worktrees and checkpoints; roles, verifier and GitHub are controlled."""
    from test_harness_pr_delivery import Remote, command
    from chatcopilot.harness import delivery

    store, ident, repo = repair
    command(repo, "branch", "-M", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)], check=True, capture_output=True
    )
    remote = Remote(bare)
    base = store.get(ident)["base_commit"]
    store.update(
        ident,
        delivery={
            "version": 1,
            "state": "pending",
            "repository": "acme/project",
            "actor": "test-actor",
            "base_branch": "main",
            "base_sha": base,
            "author_name": "Lingye",
            "author_email": "616202172@qq.com",
            "auto_merge": False,
        },
    )
    monkeypatch.setattr(delivery, "publication_checks", lambda _store, _ident, cancel: cancel())

    class FrozenTestVerifier(Verifier):
        def prepare(self, *args):
            source, plan = super().prepare(*args)
            output = args[2]
            content = b"from chatcopilot.core.probe import VALUE\ndef test_target():\n    assert VALUE == 1\n"
            path = output / "frozen_test.py"
            path.write_bytes(content)
            path.chmod(0o600)
            digest = hashlib.sha256(content).hexdigest()
            return {
                **source,
                "test_path": str(path),
                "test_sha256": digest,
                "test_relative_path": f"tests/unit/harness_regressions/test_{digest}.py",
            }, plan

    verifier, runner = FrozenTestVerifier(), Roles()
    result = run_task(
        store,
        ident,
        verifier,
        runner,
        prepare=lambda budget: delivery.initialize(store, ident, remote),
        deliver=lambda: delivery.reconcile(store, ident, client=remote),
    )
    assert result["status"] == "fixed", result.get("message")
    assert result["delivery"]["state"] == "waiting_checks", result["delivery"]
    assert remote.creations == 1 and result["graph"]["next"] == []
    assert result["delivery_graph"]["waiting"]
    remote.mark_merged(result["delivery"])
    merged = delivery.reconcile(store, ident, client=remote)
    assert merged["delivery"]["state"] == "merged" and remote.creations == 1
    assert runner.calls == 1 and command(repo, "rev-parse", "main") == base
