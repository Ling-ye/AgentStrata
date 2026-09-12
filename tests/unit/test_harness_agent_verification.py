from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.core.candidate_configuration import validate_configuration
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.evals.agent_case import SCHEMA, SUITE, case_identity, evaluation_cases, validate_case
from chatcopilot.evals.application.result_store import EvaluationResultStore
from chatcopilot.evals.conditions import verify_conditions
from chatcopilot.evals.evaluations import parse_evaluation_request, run_evaluation
from chatcopilot.evals.frozen_agent_scoring import score
from chatcopilot.evals.models import TrialObservation
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.models import CandidateRef, HarnessError, RepairFeedback, RepairOptions
from chatcopilot.harness.verification import result_from_trials
from test_case_harness import FakeCoder, FakeEvaluator, repository as repository_fixture, run_task, task_fixture

ROOT = Path(__file__).resolve().parents[2]
repository = repository_fixture


def declaration(**changes):
    return validate_case({"schema": SCHEMA, "title": "Read the fixture", "input": "Read note.txt and report its value",
                         "expected_behavior": "Report the actual file value", "fixtures": {"note.txt": "sample-value"},
                         "assertions": [{"kind": "final_contains", "value": "sample-value"}], **changes})


@pytest.mark.parametrize("outcome,error", [
    ("error", {"stage": "scoring", "code": "judge_error"}),
    ("error", {"stage": "execution", "code": "execution_error", "message": "llm_error"}),
    ("skipped", None),
])
def test_invalid_baseline_never_starts_product_repair(repository, tmp_path, outcome, error):
    class Evaluator(FakeEvaluator):
        def run(self, *args):
            value = super().run(*args)
            if args[2].endswith("-reproduce"):
                for trial in value["result"]["trials"]:
                    trial.update(outcome=outcome, error=error)
            return value
    evaluator, coder = Evaluator(current_passes=True), FakeCoder(("unrelated",))
    controller, ident = task_fixture(repository, tmp_path, evaluator)
    result = run_task(controller.store, ident, evaluator, coder)
    assert result["status"] == "blocked", result.get("message")
    assert coder.calls == 0
    assert result["evaluations"]["reproduce"]["error"]
    assert result["evaluations"]["reproduce"]["target_trials"][0]["outcome"] == outcome


def test_trial_identity_and_failure_category_are_independent(tmp_path):
    receipt = {"result": {"trials": [{"case_id": "x", "target_id": "t", "attempt": 1,
                                         "outcome": "failed"}]}}
    result = result_from_trials(receipt, "t", CandidateRef(tmp_path, "digest", "base"))
    result.require_valid(["x"], 1)
    assert not result.passed
    with pytest.raises(HarnessError, match="身份"):
        result.require_valid(["x"], 3)


def test_case_registration_is_content_addressed_and_survives_restart(tmp_path):
    case = declaration()
    store = EvaluationResultStore(tmp_path)
    first = store.register_case(case)
    assert store.register_case(copy.deepcopy(case)) == first
    assert EvaluationResultStore(tmp_path).frozen_case(first["snapshot_id"]) == first
    assert store.register_case(declaration(context="different context"))["snapshot_id"] != first["snapshot_id"]
    changed = copy.deepcopy(first)
    changed["case"]["fixtures"]["note.txt"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        evaluation_cases(changed)


@pytest.mark.parametrize("changes", [{"fixtures": {"../outside": "data"}},
    {"assertions": [{"kind": "python", "code": "assert True"}]}, {"semantic": "false"},
    {"assertions": []}, {"grading_module": "untrusted"}])
def test_untrusted_case_controls_are_rejected(changes):
    with pytest.raises(ValueError):
        declaration(**changes)


def test_real_agent_case_flows_through_standard_trial_pipeline_in_dry_run(tmp_path):
    case = declaration()
    request = {"kind": "suite", "bot": "lingye-copilot-qq", "suite": SUITE,
               "case_snapshot": {"snapshot_id": case_identity(case), "case": case},
               "repetitions": 3, "dry_run": True}
    parsed = parse_evaluation_request(request)
    assert parsed.case_snapshot["snapshot_id"] == case_identity(case)
    result = run_evaluation(request, output=tmp_path / "evaluation")
    assert result.status == "completed"
    assert len(result.trials) == 3
    assert {t.outcome for t in result.trials} == {"skipped"}
    assert len({t.trial_id for t in result.trials}) == 3


def test_scoring_uses_actual_tools_and_final_state():
    case = declaration(allowed_tools=["read_text_head"], assertions=[{"kind": "tool_called", "name": "read_text_head", "arguments": {"path": "note.txt"}},
        {"kind": "tool_result_contains", "name": "read_text_head", "value": "sample-value"},
        {"kind": "file_equals", "path": "report.txt", "value": "sample-value"}])
    case = evaluation_cases({"snapshot_id": case_identity(case), "case": case})[0]
    observation = TrialObservation(final_text="I read the file", tool_calls=(),
                                    post_state={"report.txt": {"exists": False, "text": None}})
    assert not score(case, observation)[0].passed
    observation = TrialObservation(tool_calls=({"name": "read_text_head", "arguments": {"path": "note.txt"},
        "ok": True, "result": "sample-value"},), post_state={"report.txt": {"exists": True, "text": "sample-value"}})
    assert score(case, observation)[0].passed


def test_candidate_configuration_preserves_authority_and_model():
    before = {"prompts": {"schema_version": 2, "identity": "prompts/identity.md"},
              "llm": {"chat": {"env_prefix": "EXAMPLE"}}, "agents": {"backend": "native"},
              "workspace": {"root_env": "EXAMPLE_WORKSPACE"}, "tools": {"packs": ["workspace.read_write"]}}
    candidate = copy.deepcopy(before)
    candidate["prompts"]["identity"] = "prompts/new.md"
    candidate["tools"]["packs"] = []
    def encode(value):
        return json.dumps(value).encode()
    validate_configuration(encode(before), encode(candidate))
    for key, value in [("agents", {"backend": "codex"}), ("workspace", {"root_env": "OTHER"}),
                       ("llm", {"chat": {"env_prefix": "OTHER"}})]:
        invalid = {**candidate, key: value}
        with pytest.raises(ValueError, match="candidate changes"):
            validate_configuration(encode(before), encode(invalid))


def test_comparison_allows_product_fingerprint_only_with_fixed_invariants():
    original = {"cases": {"x": "hash"}, "grading": {}, "environment": "env",
                "configuration_invariants": "same", "targets": {"t": {"model": "m", "backend": "native", "config_fingerprint": "old"}}}
    candidate = copy.deepcopy(original)
    candidate["targets"]["t"]["config_fingerprint"] = "new"
    verify_conditions(original, candidate)
    candidate["targets"]["t"]["model"] = "different"
    with pytest.raises(ValueError):
        verify_conditions(original, candidate)


def test_multiple_pytest_assertions_are_frozen_and_reused(tmp_path):
    from chatcopilot.core.source_snapshot import git_output
    root = private_directory(tmp_path / "repo")
    git_output(root, "init", "--quiet")
    (root / "src").mkdir()
    (root / "src/probe.py").write_text("VALUE = 0\n")
    (root / "tests/unit").mkdir(parents=True)
    (root / "tests/unit/test_existing.py").write_text("def test_existing(): assert True\n")
    verifier = LocalVerifier(private_directory(tmp_path / "private"))
    task = {"task_id": "repair-multiple", "review_and_commit": True,
            "source": {"kind": "robot_task", "target_id": "local-pytest", "repetitions": 1}}
    def prepare(_root, _evidence, _options, output, _check):
        draft = private_directory(output / "draft")
        (draft / "diagnosis.json").write_text(json.dumps({"reproducible": True, "reason": "fixture", "expected_behavior": "one"}))
        (draft / "test_reproduction.py").write_text("from probe import VALUE\ndef test_one(): assert VALUE == 1\ndef test_two(): assert VALUE > 0\n")
        for file in draft.iterdir():
            file.chmod(0o600)
        return {}
    task["source"] = verifier.prepare(task, root, SimpleNamespace(prepare=prepare), RepairOptions("test-model"), lambda: None)
    assert len(task["source"]["reproduction_ids"]) == 2
    before = verifier.run(task, root, "before", task["source"]["case_ids"], lambda: None)
    assert [t["outcome"] for t in before["result"]["trials"]].count("failed") == 2
    (root / "src/probe.py").write_text("VALUE = 1\n")
    after = verifier.run(task, root, "after", task["source"]["case_ids"], lambda: None)
    assert {t["outcome"] for t in after["result"]["trials"]} == {"passed"}
    assert len(verifier.regressions({"task_id": "repair-regressions"}, root, lambda: None)["passed_cases"]) == 1


def test_evaluation_hint_does_not_replace_the_frozen_expectation(repository, tmp_path):
    from chatcopilot.harness.api import HarnessController
    c = HarnessController(repository, root=tmp_path / "private", evaluator=FakeEvaluator())
    result = c.start("e", "sample-suite:b", "main", RepairOptions("test-model"),
                     feedback=RepairFeedback(repair_hint="check parsing"), launch=False)
    assert c.store.get(result["task_id"])["source"]["feedback"] == {"repair_hint": "check parsing"}
    with pytest.raises(ValueError, match="评分预期"):
        c.start("e", "sample-suite:b", "main", RepairOptions("test-model"),
                feedback=RepairFeedback(expected_behavior="different"), launch=False)


def test_case_registration_executes_via_public_service_and_standard_storage(tmp_path):
    from test_evaluation_service_protocol import _running_service, _wait_for_terminal
    with _running_service(tmp_path) as service:
        snapshot = service.client.register_case(declaration())
        assert service.client.frozen_case(snapshot["snapshot_id"]) == snapshot
        assert service.client.register_case(declaration()) == snapshot
        record = service.client.start(bot_id="lingye-copilot-qq", evaluation_id="eval-frozen-case",
            request={"kind": "suite", "suite_id": SUITE, "case_snapshot_id": snapshot["snapshot_id"],
                     "dry_run": True, "repetitions": 3})
        result = _wait_for_terminal(service.client, record["evaluation_id"], timeout_seconds=40)
        assert result["status"] == "completed", result.get("error")
        assert result["result_storage"] == "database"
        assert len(result["result"]["trials"]) == 3
        assert {t["case_id"] for t in result["result"]["trials"]} == {snapshot["snapshot_id"]}


def test_agent_input_never_receives_grading_oracle(tmp_path, monkeypatch):
    from chatcopilot.evals import frozen_agent_runtime as module
    from chatcopilot.evals.trial_capture import capture
    from chatcopilot.contracts.agent import AgentResult
    from chatcopilot.contracts.prompt import BotPromptProfile
    seen = {}
    runtime = SimpleNamespace(spec=SimpleNamespace(llm=SimpleNamespace(env_prefix="EXAMPLE")),
        tool_packs=(), subagents=object(), prompt_profile=BotPromptProfile(identity="candidate identity", response_style="concise"),
        agent_backend="native", capability_policies=(), skills=())
    class Agent:
        def new_session(self, **kwargs):
            seen["prompt"] = kwargs["prompt_input"]
            return self
        def run_task(self, task, **kwargs):
            seen["task"] = task
            return AgentResult(final_text="actual output", stop_reason="end_turn")
        def close(self):
            seen["closed"] = True
    monkeypatch.setattr(module, "load_evaluation_runtime", lambda _: runtime)
    monkeypatch.setattr(module, "load_config", lambda **_: object())
    monkeypatch.setattr(module, "_isolated_subagents", lambda value: value)
    monkeypatch.setattr(module, "assemble_agent_runtime", lambda *_, **__: Agent())
    data = declaration(context="previous user context", expected_behavior="ORACLE_SENTINEL")
    case = evaluation_cases({"snapshot_id": case_identity(data), "case": data})[0]
    with capture():
        result = module.run(case, bot="sample", workspace_root=tmp_path)
    assert result.final_text == "actual output"
    assert seen["task"].text == data["input"] and seen["task"].turn_context == data["context"]
    assert "ORACLE_SENTINEL" not in repr(seen["prompt"])
    assert "ORACLE_SENTINEL" not in repr(seen["task"])
    assert seen["prompt"].profile.identity == "candidate identity"
    assert seen["closed"]


def test_model_confirmation_failure_prevents_acceptance(repository, tmp_path):
    from test_case_harness import source_record
    class Evaluator(FakeEvaluator):
        def source(self, *args):
            return {**source_record(), "kind": "evaluation", "executor": "agent_configured", "repetitions": 3}
        def run(self, task, worktree, identifier, cases, check_cancel):
            value = super().run(task, worktree, identifier, cases, check_cancel)
            rows = value["result"]["trials"]
            for case in cases:
                rows.append({**next(row for row in rows if row["case_id"] == case), "attempt": 3})
            if "confirm" in identifier:
                for row in rows:
                    if row["case_id"] == "b":
                        row["outcome"] = "failed"
            return value
    evaluator, coder = Evaluator(), FakeCoder()
    controller, identifier = task_fixture(repository, tmp_path, evaluator, attempts=1)
    result = run_task(controller.store, identifier, evaluator, coder)
    assert result["status"] == "failed", result.get("message")
    assert result["verification_plan"]["repetitions"] == 3
    assert result["evaluations"]["verify-1"]["passed_cases"] == ["a", "b"]
    assert result["evaluations"]["confirm-1"]["failed_cases"] == ["b", "c"]
    assert not result.get("local_commit")
