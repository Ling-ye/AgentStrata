"""Offline remote-boundary fixture for existing repair/diagnosis unit suites.

These suites exercise repair, not GitHub publication. Real local/bare-remote lifecycle
and publication fault injection live in test_harness_pr_delivery.py.
"""
from __future__ import annotations

import pytest

from chatcopilot.core.source_snapshot import git_output
from chatcopilot.harness import delivery


@pytest.fixture(autouse=True)
def offline_harness_delivery(monkeypatch):
    def baseline(repository, _settings):
        return {"version": 1, "state": "pending", "repository": "acme/project", "actor": "test-actor",
                "base_branch": "main", "base_sha": git_output(repository, "rev-parse", "HEAD"),
                "author_name": "Lingye", "author_email": "616202172@qq.com", "auto_merge": False}

    class Client:
        def verify_target(self, _state):
            pass

        def fetch(self, repository, _directory, sha):
            git_output(repository, "cat-file", "-e", sha + "^{commit}")

    original = delivery.initialize

    def initialize(store, task_id, client=None):
        if store.get(task_id).get("delivery"):
            original(store, task_id, client=Client())

    from chatcopilot.harness import delivery_runtime
    monkeypatch.setattr(delivery_runtime, "launch_delivery", lambda *_args: None)
    from chatcopilot.harness.worker_runtime import SystemdWorkerControl
    from chatcopilot.harness.control_types import DispatchResult, WorkerState
    monkeypatch.setattr(SystemdWorkerControl, "observe", lambda *_args: WorkerState.INACTIVE)
    monkeypatch.setattr(SystemdWorkerControl, "launch_delivery", lambda *_args: DispatchResult("scheduled"))
    monkeypatch.setattr(delivery, "remote_baseline", baseline)
    monkeypatch.setattr(delivery, "initialize", initialize)
    monkeypatch.setattr(delivery, "reconcile", lambda store, task_id, **_kwargs: store.get(task_id))


def frozen_test_source(task, worktree):
    import hashlib
    content = b"def test_frozen_fixture():\n    assert True\n"
    sha = hashlib.sha256(content).hexdigest()
    path = worktree.parent / "frozen_fixture.py"
    path.write_bytes(content)
    path.chmod(0o600)
    return {**task["source"], "test_sha256": sha, "test_path": str(path),
            "test_relative_path": f"tests/unit/harness_regressions/test_{sha}.py"}


def approve_fixture(*_args):
    return {"decision": "approved", "problem": "", "reason": "controlled review fixture",
            "evidence_refs": ["source", "patch", "verification"]}
