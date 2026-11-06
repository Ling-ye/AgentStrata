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


def test_predecessor_checks_require_the_accepted_application_and_unchanged_test(repair, tmp_path, monkeypatch):
    from contextlib import nullcontext
    import hashlib
    from types import SimpleNamespace
    from chatcopilot.harness import activation_runtime
    from chatcopilot.harness.improvement_runtime import predecessor_checks

    _store, _ident, repo = repair
    content = b"def test_previous_feature():\n    assert True\n"
    file = tmp_path / "accepted-test.py"
    file.write_bytes(content)
    file.chmod(0o600)
    digest = hashlib.sha256(content).hexdigest()
    previous = {"source": {"test_path": str(file), "test_sha256": digest}}
    release = SimpleNamespace(bot_id="fixture", receipt={"candidate_digest": baseline(repo)["source_digest"]})
    host = SimpleNamespace(repository=repo, store=SimpleNamespace(get=lambda _: previous),
                           lifecycle=SimpleNamespace(operation=lambda _: nullcontext()))
    monkeypatch.setattr(activation_runtime, "release_source", lambda *_: release)
    assert predecessor_checks(host, "fixture", "first") == {"regression_tests": [
        {"sha256": digest, "content": content.decode(), "source_task": "first"}], "regression_cases": []}
    with pytest.raises(HarnessError, match="基线不一致"):
        predecessor_checks(host, "another", "first")
    (repo / "src/chatcopilot/core/probe.py").write_text("VALUE = 42\n")
    with pytest.raises(HarnessError, match="基线不一致"):
        predecessor_checks(host, "fixture", "first")
    release.receipt["candidate_digest"] = baseline(repo)["source_digest"]
    file.write_bytes(content + b"# changed\n")
    with pytest.raises(HarnessError, match="测试已变化"):
        predecessor_checks(host, "fixture", "first")


def test_inherited_checks_run_against_both_versions_without_changing_the_baseline(tmp_path):
    import hashlib
    from chatcopilot.core.source_snapshot import source_manifest
    from chatcopilot.harness.local_verifier import LocalVerifier
    from chatcopilot.harness.models import CandidateRef
    from chatcopilot.harness.verification import CaseVerification

    repo = tmp_path / "application"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    code = repo / "feature.py"
    code.write_text("VALUE = 1\n")
    content = "from feature import VALUE\ndef test_existing_feature():\n    assert VALUE == 1\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    task = {"task_id": "inherited", "source": {"kind": "application", "regression_tests": [
        {"sha256": digest, "content": content, "source_task": "first"}]}}
    local = LocalVerifier(tmp_path / "verification")
    verifier = CaseVerification(None, local, None)
    before = source_manifest(repo)
    reference = CandidateRef(repo, "controlled", "controlled")
    baseline_result = verifier.regressions(task, reference, lambda: None)
    assert len(baseline_result["passed_cases"]) == 1 and baseline_result["failed_cases"] == []
    assert source_manifest(repo) == before
    assert not (repo / "tests").exists()
    code.write_text("VALUE = 2\n")
    candidate = verifier.regressions(task, reference, lambda: None, baseline_result["case_ids"])
    assert candidate["failed_cases"] == baseline_result["passed_cases"]
    with pytest.raises(HarnessError, match="未通过当前基线"):
        verifier.regressions(task, reference, lambda: None)
    task["source"]["regression_tests"][0]["content"] += "# changed"
    with pytest.raises(HarnessError, match="继承的已验收测试内容已变化"):
        local.regressions(task, repo, lambda: None)


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
        settings = {"AGENTSTRATA_LLM_CONFIG": "fixture-selected-model-settings"}
        def __init__(self, *args, **kwargs):
            pass
        def start_improvement(self, request, options, **kwargs):
            called.append((request, options, kwargs))
            return {"task_id": "fixture"}
    monkeypatch.setattr(cli, "HarnessController", Controller)
    def worker_profile(_, *, environment):
        assert environment["AGENTSTRATA_LLM_CONFIG"] == Controller.settings["AGENTSTRATA_LLM_CONFIG"]
        return SimpleNamespace(model="worker", reasoning_effort="high", profile_id="worker-profile"), {}
    monkeypatch.setattr("chatcopilot.core.model_settings.worker_profile", worker_profile)
    assert cli.main(["--repository-root", str(tmp_path), command, "--bot", "fixture", "--goal", "Add an action",
                     "--allow-path", "src/chatcopilot/agent", "--request-id", "stable"]) == 0
    assert called[0][0].task_type == command and called[0][1].model == "worker"
    assert called[0][2]["request_id"] == "stable"
    assert "fixture" in capsys.readouterr().out


@pytest.mark.parametrize("retained", [False, True])
def test_agent_goal_keeps_business_input_and_the_complete_application_suite(tmp_path, retained):
    import json
    from copy import deepcopy
    from chatcopilot.core.private_sqlite import private_directory
    from chatcopilot.evals.agent_case import case_identity, validate_case
    from chatcopilot.harness.improvement_verification import ImprovementVerification
    from chatcopilot.harness.local_verifier import LocalVerifier
    from chatcopilot.harness.models import CandidateRef, VerificationRequest
    from chatcopilot.harness.preparation import acceptance
    from test_live2d_goal_replay import declaration

    data = declaration()
    output = private_directory(tmp_path / "proposal")
    draft = private_directory(output / "draft") / "agent_case.json"
    draft.write_text(json.dumps(data))
    draft.chmod(0o600)
    source = application_source(goal_case_ids=[], case_ids=["protected"], goal="Add the work cue behavior",
                                original_input="Add the work cue behavior")
    earlier = declaration(input="earlier cue")
    earlier_id = case_identity(earlier)
    if retained:
        source["regression_cases"] = [{"case": earlier, "snapshot_id": earlier_id, "source_task": "previous"}]
    task = {"task_id": "new-goal", "source": source, "acceptance": acceptance(source)}
    class Store:
        def get(self, _):
            return task
        def update(self, _, **values):
            task.update(values)
            return task
    class Evaluator:
        bad_rules = False
        broken_suite = False
        broken_retained = False
        calls = []
        def capabilities(self):
            return {"schema": "agentstrata.agent-case/v1"}
        validate_agent_case = staticmethod(validate_case)
        def prepare_agent_case(self, source, cancel):
            case = source["agent_case"]
            return {**source, "case_ids": [case_identity(case)], "case_snapshot_id": case_identity(case),
                    "conditions": None, "target_id": "", "repetitions": 3}
        def run(self, current, _path, run, checks, cancel):
            self.calls.append((run, tuple(checks)))
            return {"evaluation_id": run, "target_id": "model", "conditions": {"frozen": True}, "result": {
                "config_snapshot": {"version_snapshot": {"runtime": {"id": "fixed"},
                    "verification_sha256": "changed" if self.bad_rules else "fixed"}},
                "trials": [{"case_id": name, "target_id": "model", "attempt": attempt,
                    "outcome": "failed" if (self.broken_suite and name == "protected" or self.broken_retained and name == earlier_id) else "passed",
                    "execution": {"metadata": {"runtime_replay": {"layers": ["gateway", "application", "agent"]}}}}
                    for name in checks for attempt in range(1, 4)]}}
    evaluator = Evaluator()
    verifier = ImprovementVerification(evaluator, LocalVerifier(tmp_path / "local"), Store())
    candidate = CandidateRef(tmp_path, "candidate", "base")
    prepared, plan = verifier.prepare(task, candidate, output,
        {"verification_kind": "agent", "summary": "actual dialogue", "coverage": [
            {"requirement": "expected_behavior", "checks": ["agent_case"]}]}, lambda: None)
    assert prepared["original_input"] == source["original_input"]
    assert prepared["agent_source"]["agent_case"]["input"] == "work cue"
    assert prepared["agent_source"]["agent_case"]["context"] == ""
    expectation = prepared["agent_source"]["agent_case"]["expected_behavior"]
    assert expectation.endswith(source["original_input"])
    assert "运行行为" in expectation and "不要要求运行中的 Agent 再次开发软件" in expectation
    # The development goal stays in the judge's expectation, never in the actual turn.
    assert source["original_input"] not in prepared["agent_source"]["agent_case"]["input"]
    assert task["acceptance"]["original"] == source["original_input"]
    assert plan.primary_checks == (prepared["agent_source"]["case_snapshot_id"],)
    assert set(plan.checks) == {"protected", *plan.primary_checks, *([earlier_id] if retained else [])}
    assert plan.protected_checks == ((earlier_id,) if retained else ())
    task.update(source=prepared, verification_plan=plan.to_payload())
    result = verifier.run(VerificationRequest.from_task(task), candidate, "first", list(plan.checks), lambda: None)
    result.require_valid(plan.checks, 3)
    assert result.passed
    for field in ("agent_source", "original_case_source"):
        assert task["source"][field]["version_conditions"]["verification_sha256"] == "fixed"
    evaluator.broken_suite = True
    rejected = verifier.run(VerificationRequest.from_task(task), candidate, "candidate", list(plan.checks), lambda: None)
    assert "protected" not in rejected.passed and {c.check_id for c in rejected.checks if c.outcome == "failed"} == {"protected"}
    evaluator.bad_rules = True
    with pytest.raises(HarnessError, match="版本发生变化"):
        verifier.run(VerificationRequest.from_task(deepcopy(task)), candidate, "drift", list(plan.checks), lambda: None)
    if retained:
        evaluator.bad_rules = evaluator.broken_suite = False
        evaluator.broken_retained = True
        task["delivery"] = {"source_digest": "candidate"}
        with pytest.raises(HarnessError, match="Agent 用例未通过当前基线"):
            verifier.run(VerificationRequest.from_task(task), candidate, "inherited-baseline", list(plan.checks), lambda: None)


def test_accepted_agent_case_is_retained_and_tampering_is_rejected(repair, tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from chatcopilot.evals.agent_case import case_identity
    from chatcopilot.harness import activation_runtime
    from chatcopilot.harness.improvement_runtime import predecessor_checks
    from test_live2d_goal_replay import declaration

    _, _, repo = repair
    case = declaration()
    previous = {"source": {"agent_source": {"agent_case": case, "case_snapshot_id": case_identity(case)}}}
    release = SimpleNamespace(bot_id="fixture", receipt={"candidate_digest": baseline(repo)["source_digest"]})
    host = SimpleNamespace(repository=repo, store=SimpleNamespace(get=lambda _: previous),
        lifecycle=SimpleNamespace(operation=lambda _: nullcontext()),
        evaluator=SimpleNamespace(prepare_agent_case=lambda source, cancel: {"case_snapshot_id": case_identity(source["agent_case"])}))
    monkeypatch.setattr(activation_runtime, "release_source", lambda *_: release)
    checks = predecessor_checks(host, "fixture", "accepted")
    assert checks == {"regression_tests": [], "regression_cases": [
        {"snapshot_id": case_identity(case), "case": case, "source_task": "accepted"}]}
    case["input"] = "another trigger"
    with pytest.raises(HarnessError, match="Agent 用例已变化"):
        predecessor_checks(host, "fixture", "accepted")
