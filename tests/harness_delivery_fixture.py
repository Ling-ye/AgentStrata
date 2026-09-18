"""Offline remote-boundary fixture for existing repair/diagnosis unit suites.

These suites exercise repair, not GitHub publication. Real local/bare-remote lifecycle
and publication fault injection live in test_harness_pr_delivery.py.
"""
from __future__ import annotations

from types import SimpleNamespace
import pytest
from pathlib import Path

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

    import sys
    from chatcopilot.harness import task_environment
    monkeypatch.setattr(task_environment, 'prepare_environment', lambda *args: {'python': sys.executable})
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


def freeze_fixture(verifier, task, root, author, options, cancel):
    """Produce one test draft, then exercise the v2 host's single-submission boundary."""
    import json
    from chatcopilot.core.private_sqlite import private_directory
    from chatcopilot.evals.agent_case import validate_case
    from chatcopilot.harness.preparation import acceptance
    output = private_directory(verifier.root / "jobs" / task["task_id"] / "test-submission")
    author.prepare(root, {"source": task["source"]}, options, output, cancel)
    diagnosis = json.loads((output / "draft/diagnosis.json").read_text())
    task["source"].setdefault("original_input", diagnosis.get("expected_behavior", "fixture"))
    task["acceptance"] = acceptance(task["source"])
    verifier.validate_case = validate_case
    proposal = {"verification_kind": diagnosis.get("verification_kind", "pytest"), "summary": diagnosis["reason"],
                "coverage": [{"requirement": name, "checks": checks} for name, checks in diagnosis.get("coverage", {}).items()]}
    return verifier.prepare(task, root, output, proposal, cancel)


def candidate_submission():
    return {"submission": {"decision": "candidate", "summary": "controlled candidate", "verification_kind": "pytest",
        "goal_capabilities": [], "coverage": [{"requirement": "expected_behavior", "checks": ["reproduction"]}], "gaps": [], "notes": []}}


class RoleFixture:
    """Controlled role backend for tests retaining product/verification fixtures."""
    def execute(self, root, call, options, output, cancel):
        from chatcopilot.harness.agent_types import AgentResult, Role
        from chatcopilot.core.private_sqlite import private_directory
        import json
        output = private_directory(output)
        evidence = dict(call.evidence)
        original = evidence.get('original_source', {})
        if original.get('path'):
            evidence['source'] = json.loads(Path(original['path']).read_text())
        if call.role == Role.MAIN:
            value = {'next_role': 'plan', 'summary': 'investigate cause', 'unresolved': []}
        elif call.role == Role.PLAN:
            value = {'decision': 'proceed', 'summary': 'controlled root cause', 'evidence_refs': ['source'], 'changes': ['product'],
                     'verification_order': 'code_first', 'goal_capabilities': [], 'unresolved': []}
        elif call.role == Role.CODING:
            result = self.run(root, evidence, options, output, cancel)
            self._role_submission = result.get('submission') or candidate_submission()['submission']
            value = {'summary': self._role_submission['summary'], 'notes': [], 'needs_replan': False, 'gaps': self._role_submission.get('gaps', [])}
        elif call.role == Role.TEST:
            if hasattr(self, 'prepare'):
                self.prepare(root, evidence, options, output, cancel)
            value = {"notes": [], **self._role_submission}
        else:
            value = self.review(root, evidence, options, output, cancel)
        return AgentResult(value, {})




class RoleNamespace(RoleFixture, SimpleNamespace):
    pass
