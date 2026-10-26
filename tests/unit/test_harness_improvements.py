"""User goals reuse the repair graph without publishing or touching operator files."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess

import pytest

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.improvement_types import ImprovementRequest
from chatcopilot.harness.improvement_repository import baseline, initialize
from chatcopilot.harness.models import HarnessError, RepairOptions
from chatcopilot.harness.repair_runtime import run_task
from chatcopilot.harness.workspace import delta
from test_harness_repair_v2 import Coder, Verifier, repair as repair_fixture


@pytest.fixture
def repair(tmp_path):
    return repair_fixture.__wrapped__(tmp_path)


def application_source(**updates):
    return {"kind": "application", "task_type": "improve", "bot_id": "fixture", "goal": "Return one",
        "original_input": "Return one", "allowed_paths": ["src/chatcopilot/core/probe.py"],
        "suite_id": "agentstrata-application-bridges-v1", "goal_case_ids": ["target"],
        "case_ids": ["target"], "target_id": "main", "conditions": {"cases": {"target": "fixed"}},
        **updates}


class GoalVerifier(Verifier):
    def prepare(self, *args):
        source, plan = super().prepare(*args)
        return source, replace(plan, purpose="improvement")


def test_goal_graph_freezes_dirty_baseline_and_only_exports_candidate(repair):
    store, ident, repo = repair
    (repo / "src/chatcopilot/core/probe.py").write_text("VALUE = 2\n")
    index = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--stage"])
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    store.update(ident, source=application_source(), delivery=baseline(repo))
    previous = os.umask(0o022)
    try:
        initialize(store, ident)
    finally:
        os.umask(previous)
    result = run_task(store, ident, GoalVerifier(), Coder())
    assert result["status"] == "fixed", result.get("message")
    assert result["accepted_candidate"]["candidate_digest"] == result["verified_digest"]
    assert result["verification_plan"]["purpose"] == "improvement"
    assert (repo / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 2\n"
    assert subprocess.check_output(["git", "-C", str(repo), "ls-files", "--stage"]) == index
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]) == head
    assert result["delivery"]["state"] == "local"
    attempt = store.attempts(ident)[0]
    patch = (store.root / "jobs" / ident / attempt["patch_path"]).read_text()
    assert "-VALUE = 2" in patch and "+VALUE = 1" in patch


def test_goal_scope_rejects_product_changes_outside_user_scope(repair):
    store, ident, repo = repair
    store.update(ident, source=application_source(), delivery=baseline(repo))
    initialize(store, ident)
    task = store.get(ident)
    root = Path(task["worktree"])
    (root / "src/chatcopilot/core/unrelated.py").write_text("VALUE = 1\n")
    with pytest.raises(HarnessError, match="候选修改"):
        delta(root, task["baseline_manifest"], "fixture", application=True,
              scope=("src/chatcopilot/core/probe.py",), frozen=store.root / "jobs" / ident / "source")


def test_goal_without_behavior_difference_cannot_be_accepted(repair):
    store, ident, repo = repair
    (repo / "src/chatcopilot/core/probe.py").write_text("VALUE = 1\n")
    store.update(ident, source=application_source(), delivery=baseline(repo))
    initialize(store, ident)
    result = run_task(store, ident, GoalVerifier(), Coder())
    assert result["status"] == "not_reproduced"
    assert not result.get("accepted_candidate")


def test_textual_change_with_no_measured_goal_difference_stays_unaccepted(repair):
    store, ident, repo = repair
    (repo / "src/chatcopilot/core/probe.py").write_text("VALUE = 1  # same behavior\n")
    store.update(ident, source=application_source(), delivery=baseline(repo))
    initialize(store, ident)
    result = run_task(store, ident, GoalVerifier(), Coder())
    assert result["status"] == "needs_review"
    assert result["verification_gaps"][0]["requirement"] == "expected_behavior"
    assert not result.get("accepted_candidate")


def test_local_goal_start_is_idempotent_and_never_preflights_github(repair, monkeypatch):
    _store, _ident, repo = repair
    class Evaluator:
        def application_source(self, bot_id, suite_id):
            return {"case_ids": ["target"], "target_id": "main", "runtime_id": "langgraph",
                    "conditions": {"cases": {"target": "frozen"}}, "request": {"kind": "suite", "suite_id": suite_id}}
    class Workers:
        def observe(self, task):
            return WorkerState.INACTIVE
    monkeypatch.setattr("chatcopilot.harness.delivery.remote_baseline", lambda *args: pytest.fail("GitHub was used"))
    controller = HarnessController(repo, root=repo.parent / "goal-control", evaluator=Evaluator(), worker_control=Workers())
    request = ImprovementRequest("integrate", "fixture", "Connect the existing tool", ("src/chatcopilot/core/probe.py",), ("target",))
    first = controller.start_improvement(request, RepairOptions("fixture"), request_id="one", launch=False)
    assert first["status"] == "queued", first
    assert controller.start_improvement(request, RepairOptions("fixture"), request_id="one", launch=False)["task_id"] == first["task_id"]
    with pytest.raises(HarnessError, match="同一请求"):
        controller.start_improvement(replace(request, goal="Different goal"), RepairOptions("fixture"), request_id="one", launch=False)
    cancelled = controller.cancel(first["task_id"])
    assert cancelled["status"] == "cancelled" and cancelled["delivery"]["state"] == "local"
    controller.lifecycle.prepare_resume_locked(first["task_id"])
    resumed, claimed = controller.lifecycle.claim_resume_locked(first["task_id"])
    assert claimed and resumed["delivery"]["state"] == "local"


@pytest.mark.parametrize("scope", [("../outside",), (".",), ("/absolute",), ("src//duplicate",)])
def test_goal_rejects_noncanonical_scope(scope):
    with pytest.raises(ValueError):
        ImprovementRequest("improve", "fixture", "goal", scope)


def test_goal_verification_binds_the_whole_suite_and_preserves_frozen_conditions():
    from types import SimpleNamespace
    from chatcopilot.harness.improvement_verification import ImprovementVerification
    from chatcopilot.harness.models import VerificationRequest, CandidateRef
    from chatcopilot.harness.integrations.scenarios import ImprovementScenario
    from test_harness_repair_v2 import proposal
    source = application_source(case_ids=["target", "qq-protection"], repetitions=3,
                                request={"kind": "suite"})
    verifier = ImprovementVerification(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    prepared, plan = verifier.prepare(VerificationRequest("task", source, {}, None, False),
        CandidateRef(Path("/tmp/candidate"), "digest", "base"), Path("/tmp/output"),
        proposal(verification_kind="existing"), lambda: None)
    assert plan.primary_checks == ("target",)
    assert plan.checks == ("target", "qq-protection")
    assert plan.check_repetitions == {"target": 3, "qq-protection": 3}
    assert plan.real_agent and plan.purpose == "improvement"
    fresh = {**prepared, "agent_source": {**prepared["agent_source"], "conditions": {"tampered": True}}}
    retained = ImprovementScenario().retain_conditions(fresh, prepared)
    assert retained["agent_source"]["conditions"] == source["conditions"]


def test_goal_source_freezes_target_through_read_only_evaluation_preview():
    from types import SimpleNamespace
    from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
    calls = []
    def preview(**kwargs):
        calls.append(kwargs)
        return {"ready": True, "targets": [{"target_id": "main", "runtime_id": "codex",
            "executor": "agent_configured", "model": "chosen"}], "conditions": {"cases": {"target": "hash"}}}
    value = ServiceEvaluator(SimpleNamespace(preview=preview)).application_source("fixture", "suite")
    assert calls == [{"bot_id": "fixture", "request": value["request"]}]
    assert value["conditions"]["cases"] == {"target": "hash"}
    assert value["evidence"]["run"] == {"runtime_id": "codex", "model": "chosen"}


def test_pinned_upstream_material_is_read_only_content_bound_and_reused(repair, monkeypatch):
    from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope
    from chatcopilot.harness.improvement_repository import prepare_upstream
    store, ident, repo = repair
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    envelope = AdapterApprovalEnvelope("fixture", "https://github.com/example/sample", commit, "MIT fixture", "Inspect adapter API")
    source = application_source(source_envelope=envelope.canonical_payload(), source_envelope_digest=envelope.candidate_digest)
    store.update(ident, source=source)
    real_run = subprocess.run
    fetches = []
    def local_transport(command, **kwargs):
        if "fetch" in command:
            fetches.append(command)
            command = ["git", "-c", "protocol.file.allow=always", *command[1:]]
            # Override only transport in this fixture; actual Git objects, commit
            # verification, archive extraction and content checks still execute.
            command = [part.replace("protocol.file.allow=never", "protocol.file.allow=always") for part in command]
            command = [str(repo) if part == envelope.source_url else part for part in command]
        return real_run(command, **kwargs)
    monkeypatch.setattr(subprocess, "run", local_transport)
    prepare_upstream(store, ident, lambda: None)
    observed = store.get(ident)["upstream_source"]
    assert observed["commit"] == commit
    assert (Path(observed["root"]) / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 0\n"
    prepare_upstream(store, ident, lambda: None)
    assert len(fetches) == 1
    (Path(observed["root"]) / "src/chatcopilot/core/probe.py").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        prepare_upstream(store, ident, lambda: None)


@pytest.mark.parametrize("command", ["integrate", "improve"])
def test_goal_cli_uses_the_public_controller(command, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from chatcopilot.harness import __main__ as cli
    called = []
    class Controller:
        def __init__(self, *args, **kwargs):
            pass
        def start_improvement(self, request, options, **kwargs):
            called.append((request, options, kwargs))
            return {"task_id": "fixture"}
    monkeypatch.setattr(cli, "HarnessController", Controller)
    monkeypatch.setattr("chatcopilot.core.model_settings.worker_profile", lambda _: (
        SimpleNamespace(model="worker", reasoning_effort="high", profile_id="worker-profile"), {}))
    assert cli.main(["--repository-root", str(tmp_path), command, "--bot", "fixture", "--goal", "Add an action",
                     "--allow-path", "src/chatcopilot/agent", "--request-id", "stable"]) == 0
    assert called[0][0].task_type == command and called[0][1].model == "worker"
    assert called[0][2]["request_id"] == "stable"
    assert "fixture" in capsys.readouterr().out
