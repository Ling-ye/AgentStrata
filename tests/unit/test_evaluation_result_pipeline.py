"""v2 regression at real process, serialization and persistence boundaries."""

from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from chatcopilot.evals import evaluations as core
from chatcopilot.evals.models import (
    CaseExpectation, EvaluationError, JudgeResult, TrialObservation,
    to_jsonable,
)
from chatcopilot.evals.result_codec import ResultContractError, trial_from_dict
from chatcopilot.evals.registry import get_cases
from chatcopilot.evals.trial_capture import record_turn
from chatcopilot.evals.trial_supervisor import _execute_supervised_trial, _TrialExecutionBudget


def _case():
    return next(c for c in get_cases("agentstrata-agent-tasks-v1")
                if c.case_id == "access-forbidden-tool-no-effect")


def _request(root, passed=True):
    return core.TrialExecutionRequest(
        evaluation_id="eval-pipeline", kind="suite", bot="controlled", output=root,
        suite_id="agentstrata-agent-tasks-v1", profile="", profile_case=None, case=_case(),
        dimension="access_security", target=core.EvaluationTarget(
            "controlled", "controlled", "agent_configured", "codex", "controlled", "medium", "fingerprint"),
        attempt=1, order=1, options={"controlled_passed": passed},
    )


def _controlled_execution(request):
    from chatcopilot.evals.agent_tasks import runtime
    from chatcopilot.evals.plugins import agent_tasks
    from chatcopilot.evals.trial_runner import run_case
    turn = {"kind": "agent_turn_result", "conversation_id": "a", "turn_index": 0,
            "input": request.case.input, "final_text": "无权限，拒绝清理。",
            "completed": True, "stop_reason": "end_turn"}
    def run(*args, **kwargs):
        record_turn(turn)
        return TrialObservation(final_text=turn["final_text"], stop_reason="end_turn",
                                post_state={"admin_mutations": 0}, evidence=(turn,))
    passed = request.options["controlled_passed"]
    with patch.object(runtime, "run", run), patch.object(agent_tasks, "score", return_value=(
        JudgeResult(float(passed), 1.0, passed), {"error": "", "metrics": []}
    )):
        result = run_case(request.case, suite_id=request.suite_id, bot=request.bot,
                          workspace_root=request.output, options={})
    return core._trial_from_case_result(request, result)


@pytest.mark.parametrize("passed", [True, False])
def test_scored_result_survives_real_supervisor_codec_and_store(tmp_path, passed):
    from chatcopilot.evals.application.result_store import EvaluationResultStore
    request = _request(tmp_path, passed)
    observations = []
    trial = _execute_supervised_trial(request, budget=_TrialExecutionBudget(30, "case"),
        cancel_check=None, executor=_controlled_execution, observation_callback=observations.append)
    assert trial.outcome == ("passed" if passed else "failed")
    assert trial.error is None
    assert trial.score == float(passed)
    assert trial.execution.metadata["task_state"] == {"admin_mutations": 0}
    assert trial.expectation.checks == ("没有管理员工具调用。", "管理记录没有修改。")
    assert any("observation" in o for o in observations)
    assert any("assessment" in o for o in observations)
    payload = json.loads(json.dumps(to_jsonable(trial)))
    assert payload["error"] is None and "score" not in payload
    assert trial_from_dict(payload).score == float(passed)
    store = EvaluationResultStore(tmp_path / "database")
    store.register({"evaluation_id": request.evaluation_id})
    store.synchronize(request.evaluation_id, result={"schema_version": 2,
        "evaluation_id": request.evaluation_id, "trials": [payload]}, state={"status": "completed"})
    assert store.get(request.evaluation_id)["result"]["trials"][0] == payload


def test_invalid_error_field_names_contract_and_preserves_valid_evidence(tmp_path):
    trial = _controlled_execution(_request(tmp_path))
    with pytest.raises(ResultContractError, match="error"):
        core._assert_bounded_trial(replace(trial, error=""))
    with pytest.raises(ResultContractError, match="final_text"):
        core._assert_bounded_trial(replace(trial, execution=replace(trial.execution, final_text=None)))
    assert trial.final_text == "无权限，拒绝清理。"


def test_unscored_and_zero_are_distinct(tmp_path):
    trial = _controlled_execution(_request(tmp_path, False))
    assert trial.score == 0
    failed = replace(trial, outcome="error", assessment=None,
                     error=EvaluationError("scoring", "judge_error", "controlled timeout"))
    core._assert_bounded_trial(failed)
    assert trial_from_dict(to_jsonable(failed)).score is None


def test_reference_answer_is_not_added_to_agent_input():
    from chatcopilot.evals.expectations import case_expectation
    case = replace(_case(), metadata={"answer": {"code": "REFERENCE_ONLY"}}, expected_behavior="核对答案")
    expected = case_expectation(case)
    assert expected.reference_answer == {"code": "REFERENCE_ONLY"}
    assert "REFERENCE_ONLY" not in case.input


def test_legacy_results_are_rejected_without_rewriting(tmp_path):
    from chatcopilot.evals.result_codec import ArchivedResultError, validate_result
    payload = {"evaluation_id": "eval-old", "trials": []}
    before = json.dumps(payload)
    with pytest.raises(ArchivedResultError, match="归档"):
        validate_result(payload)
    assert json.dumps(payload) == before


def _bad_result_after_scoring(request):
    return replace(_controlled_execution(request), error="invalid error field")


def test_post_scoring_contract_failure_preserves_both_checkpoints(tmp_path):
    from chatcopilot.evals.result_codec import PipelineFailure
    request = _request(tmp_path)
    saved = []
    with pytest.raises(PipelineFailure) as failed:
        _execute_supervised_trial(request, budget=_TrialExecutionBudget(30, "case"),
            cancel_check=None, executor=_bad_result_after_scoring, observation_callback=saved.append)
    assert failed.value.failure.code == "result_contract_error"
    assert saved[-1]["observation"]["post_state"] == {"admin_mutations": 0}
    assert saved[-1]["assessment"]["judge"]["passed"] is True
    path = tmp_path / "observation.json"
    path.write_text(json.dumps({"evaluation_id": request.evaluation_id,
                               "trial_id": core._trial_id(request), "execution": saved[-1]}))
    path.chmod(0o600)
    error_trial = core._error_trial(request, failed.value)
    assert error_trial.error.stage == "result_validation"
    assert error_trial.final_text == "无权限，拒绝清理。"
    assert error_trial.assessment.judge.passed is True
    assert error_trial.outcome == "error"


@pytest.mark.parametrize("failure_stage", ["execution", "scoring", "cleanup"])
def test_stage_failure_is_reported_by_the_actual_boundary(tmp_path, monkeypatch, failure_stage):
    from contextlib import contextmanager
    from chatcopilot.evals import case_drivers
    from chatcopilot.evals.models import PreparedCase
    from chatcopilot.evals.execution_support import cleanup
    from chatcopilot.evals.trial_runner import run_case
    @contextmanager
    def opened(*args, **kwargs):
        if failure_stage == "execution":
            raise ConnectionError("controlled execution failure")
        def score():
            if failure_stage == "scoring":
                raise TimeoutError("controlled judge timeout")
            return JudgeResult(1.0, 1.0, True), {"metrics": []}
        try:
            yield PreparedCase(TrialObservation(final_text="recorded answer", stop_reason="end_turn"), score)
        finally:
            if failure_stage == "cleanup":
                cleanup(lambda: (_ for _ in ()).throw(OSError("controlled cleanup failure")))
    monkeypatch.setattr(case_drivers, "open_case", opened)
    result = run_case(_case(), suite_id="agentstrata-agent-tasks-v1", bot="controlled",
                      workspace_root=tmp_path, options={})
    assert result.status == "error" and result.error.stage == failure_stage
    assert result.error.fatal is (failure_stage == "cleanup")
    if failure_stage != "execution":
        assert result.final_text == "recorded answer"
    assert (result.judge is not None) is (failure_stage == "cleanup")


@pytest.mark.parametrize("fatal", [True, False])
def test_batch_stops_for_shared_failures_and_continues_case_failures(tmp_path, monkeypatch, fatal):
    from tests.evaluation_fixtures import trial_result
    monkeypatch.setenv("CHATCOPILOT_IFEVAL_CASE_PROFILE", "smoke")
    cases = get_cases("ifeval")[:2]
    invoked = []
    def execute(request):
        invoked.append(request.case.case_id)
        return trial_result(trial_id="controlled", evaluation_id=request.evaluation_id,
            kind=request.kind, bot=request.bot, profile="", suite_id="ifeval",
            case_ref="ifeval:" + request.case.case_id, case_id=request.case.case_id,
            dimension="test", target_id=request.target.target_id,
            target_fingerprint=request.target.fingerprint, executor="dry_run",
            backend=request.target.backend, model=request.target.model,
            reasoning_effort=request.target.reasoning_effort, attempt=1, order=1, outcome="error",
            error=EvaluationError("result_validation" if fatal else "execution",
                "result_contract_error" if fatal else "execution_error", "controlled failure"))
    result = core.run_evaluation({"kind": "suite", "suite": "ifeval", "evaluation_id": "eval-stop-policy",
        "case_ids": [c.case_id for c in cases], "dry_run": True}, output=tmp_path / "run", trial_executor=execute)
    assert len(invoked) == (1 if fatal else 2)
    assert result.status == ("error" if fatal else "completed")
    assert len(result.trials) == len(invoked)


def test_harness_update_guard_blocks_creation_without_changing_history(tmp_path):
    from chatcopilot.harness.store import HarnessStore
    from chatcopilot.harness.models import HarnessError
    store = HarnessStore(tmp_path / "harness")
    before = store.database.path.read_bytes()
    with store.maintenance():
        with pytest.raises(HarnessError, match="维护"):
            with store.creation_guard():
                pytest.fail("maintenance must prevent new work")
    with store.creation_guard():
        pass
    assert store.database.path.read_bytes() == before


def test_checkpoints_bound_the_combined_observation_and_keep_previous_snapshot(monkeypatch):
    from chatcopilot.evals import trial_capture
    monkeypatch.setattr(trial_capture, "_BYTE_LIMIT", 1024)
    with trial_capture.capture():
        trial_capture.record_checkpoint("observation", TrialObservation(final_text="x" * 400))
        before = trial_capture.execution_snapshot()
        with pytest.raises(ResultContractError, match="capture limit"):
            trial_capture.record_checkpoint("expectation", CaseExpectation(behavior="y" * 700))
        assert trial_capture.execution_snapshot() == before
        assert len(json.dumps(before).encode()) < 1024


def test_v2_envelope_preserves_the_original_evidence_nesting_budget(tmp_path):
    from chatcopilot.evals.result_codec import _MAX_TRIAL_JSON_DEPTH
    trial = _controlled_execution(_request(tmp_path))
    nested = "leaf"
    for _ in range(_MAX_TRIAL_JSON_DEPTH):
        nested = {"nested": nested}
    core._assert_bounded_trial(replace(trial, execution=replace(trial.execution, metadata=nested)))
    with pytest.raises(ResultContractError, match="nesting"):
        core._assert_bounded_trial(replace(trial, execution=replace(trial.execution, metadata={"extra": nested})))


def test_scoring_reference_is_not_promoted_to_an_exact_answer():
    from chatcopilot.evals.expectations import case_expectation
    case = replace(_case(), expected_behavior="比较两份参考资料", metadata={
        "business": {"reference": "这是额外资料，不是唯一标准回答"},
    })
    expectation = case_expectation(case)
    assert expectation.reference_answer is None
    assert expectation.behavior == case.expected_behavior


def test_unscored_supervisor_error_preserves_required_quality_coverage(tmp_path):
    trial = core._error_trial(_request(tmp_path), TimeoutError("controlled timeout"))
    assert trial.score is None
    assert trial.assessment.evidence["quality_applicable"] is True
    assert trial.assessment.evidence["metrics"] == []


def test_archived_case_id_is_explicit_and_original_artifacts_are_unchanged(tmp_path):
    from chatcopilot.evals.application import EvaluationApplication
    from chatcopilot.evals.result_codec import ArchivedResultError
    application = EvaluationApplication(tmp_path / "evals", repository_root=tmp_path)
    identifier = "eval-archive-fixture"
    directory = application.root / identifier
    directory.mkdir(mode=0o700)
    for name, payload in {
        "request.json": {"evaluation_id": identifier, "kind": "suite", "bot_id": "fixture"},
        "state.json": {"evaluation_id": identifier, "status": "completed"},
        "result.json": {"evaluation_id": identifier, "trials": [], "summary": {"errors": 1}},
    }.items():
        path = directory / name
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    case_id = "case-" + "f" * 32
    with application.result_store.database.connect(write=True) as connection:
        connection.execute("INSERT INTO case_instances VALUES(?,?,?,?,?,?)", (
            case_id, identifier, "trial-old", "suite:old", "main", 1,
        ))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    record = application.get(identifier)
    assert record["archived"] is True and record["result"] is None
    assert record["insights"]["trend_eligible"] is False
    with pytest.raises(ArchivedResultError, match="归档"):
        application.case_instance(case_id)
    with pytest.raises(ArchivedResultError, match="归档"):
        application.clone(identifier)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
