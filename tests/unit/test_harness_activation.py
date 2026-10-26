"""Only bound independent acceptance may enter installed-version preparation."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.harness.activation_runtime import release_source
from chatcopilot.harness.improvement_repository import baseline, initialize
from chatcopilot.harness.improvement_verification import ImprovementVerification
from chatcopilot.harness.models import CandidateRef, HarnessError, VerificationCheck, VerificationRequest, VerificationResult
from chatcopilot.harness.repair_runtime import run_task
from test_harness_improvements import application_source
from test_harness_repair_v2 import Coder, Verifier, repair as repair_fixture
from tests.application_fixture import write_application


class VersionedVerifier(Verifier):
    def __init__(self, drift=False):
        super().__init__()
        self.drift = drift

    def prepare(self, *args):
        source, plan = super().prepare(*args)
        return source, replace(plan, purpose="improvement", real_agent=True, repetitions=3,
                               check_repetitions={"target": 3})

    def run(self, task, candidate, ident, checks, cancel):
        passed = "VALUE = 1" in (candidate.path / "src/chatcopilot/core/probe.py").read_text()
        return VerificationResult(ident, candidate.digest, tuple(
            VerificationCheck("target", index, "passed" if passed else "failed", "" if passed else "product")
            for index in (1, 2, 3)), (ident,), {ident: {"snapshot": {
                "source_sha256": candidate.digest, "runtime": {"fixture": "changed" if self.drift and passed else "same"},
                "verification_sha256": "a" * 64}, "model_identity": "b" * 64}})


@pytest.fixture
def accepted_goal(tmp_path):
    store, ident, repo = repair_fixture.__wrapped__(tmp_path)
    (repo / "bots/fixture").mkdir(parents=True)
    write_application(repo / "bots/fixture/bot.yaml", {"id": "fixture", "display_name": "Fixture", "agents": {"runtime": "langgraph"}})
    store.update(ident, source=application_source(), delivery=baseline(repo))
    initialize(store, ident)
    result = run_task(store, ident, VersionedVerifier(), Coder())
    assert result["status"] == "fixed", result.get("message")
    return SimpleNamespace(store=store, repository=repo), ident


def test_accepted_goal_exports_exact_snapshot_and_runtime_identity(accepted_goal):
    controller, ident = accepted_goal
    source = release_source(controller, ident)
    assert source.receipt["candidate_digest"] == controller.store.get(ident)["verified_digest"]
    assert source.runtime_environment == {"fixture": "same"}
    assert source.model_identity == "b" * 64
    assert source.bot_relative == "bots/fixture/bot.yaml"
    assert source.candidate != Path(controller.store.get(ident)["worktree"])


@pytest.mark.parametrize("change", ["cancelled", "review", "snapshot"])
def test_cancelled_tampered_or_unreviewed_goal_cannot_activate(accepted_goal, change):
    controller, ident = accepted_goal
    task = controller.store.get(ident)
    if change == "cancelled":
        controller.store.update(ident, delivery_cancel_requested=True)
    elif change == "review":
        attempt = controller.store.attempts(ident)[0]
        attempt["review"]["decision"] = "rejected"
        controller.store.save_attempt(ident, 1, attempt)
    else:
        root = Path(controller.store.attempts(ident)[0]["snapshot_path"])
        (root / "src/chatcopilot/core/probe.py").write_text("VALUE = 3")
    with pytest.raises(HarnessError):
        release_source(controller, task["task_id"])


@pytest.mark.parametrize("change", ["runtime", "verification_sha256", "model_identity"])
def test_application_verification_rejects_execution_conditions_drift(tmp_path, change):
    source = {"kind": "application", "agent_source": {"kind": "application", "case_ids": ["goal"],
        "target_id": "main", "conditions": {"frozen": True}}}
    state = {"task_id": "fixture", "source": source, "acceptance": {}, "verification_plan": None}
    class Store:
        def get(self, _):
            return state
        def update(self, _, **values):
            state.update(values)
    class Evaluator:
        changed = False
        def run(self, task, root, identifier, cases, cancel):
            snapshot = {"runtime": {"python": "same"}, "verification_sha256": "a" * 64}
            model = "b" * 64
            if self.changed:
                if change == "model_identity":
                    model = "c" * 64
                else:
                    snapshot[change] = {"python": "other"} if change == "runtime" else "c" * 64
            return {"evaluation_id": identifier, "target_id": "main", "result": {
                "config_snapshot": {"version_snapshot": snapshot, "activation_model_identity": model},
                "trials": [{"case_id": "goal", "attempt": 1, "target_id": "main", "outcome": "passed"}]}}
    evaluator = Evaluator()
    verifier = ImprovementVerification(evaluator, SimpleNamespace(), Store())
    candidate = CandidateRef(tmp_path, "digest", "commit")
    verifier.run(VerificationRequest.from_task(state), candidate, "before", ["goal"], lambda: None)
    evaluator.changed = True
    with pytest.raises(HarnessError, match="runtime"):
        verifier.run(VerificationRequest.from_task(state), candidate, "after", ["goal"], lambda: None)


@pytest.mark.parametrize("operation", ["prepare-activation", "activate", "activation", "rollback-activation", "reconcile-activation", "cancel-activation"])
def test_activation_cli_routes_through_controller_and_reports_failed_switch(operation, monkeypatch, capsys):
    from chatcopilot.harness import __main__ as cli
    calls = []
    class Controller:
        def __init__(self, *args, **kwargs):
            pass
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                calls.append((name, args, kwargs))
                return {"kind": "activate", "state": "rolled_back", "running_matches": False}
            return invoke
    monkeypatch.setattr(cli, "HarnessController", Controller)
    code = cli.main([operation, "repair-fixture"])
    assert calls[0][0] == operation.replace("-", "_")
    assert code == (1 if operation in {"activate", "rollback-activation", "reconcile-activation"} else 0)
    assert "rolled_back" in capsys.readouterr().out
