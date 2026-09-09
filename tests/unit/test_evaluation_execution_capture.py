import json
import time

import pytest

from chatcopilot.evals import evaluations as core
from chatcopilot.evals.artifact_guard import ArtifactIntegrityGuard
from chatcopilot.evals.models import EvalCase
from chatcopilot.evals.trial_capture import capture, record_turn, set_phase


def _produce_then_wait(request):
    record_turn(
        {
            "input": "actual dynamic input",
            "final_text": "generated answer",
            "completed": True,
            "stop_reason": "end_turn",
            "conversation_id": "actor-a",
            "turn_index": 1,
        }
    )
    set_phase("judging")
    time.sleep(30)
    raise AssertionError("supervisor should have cancelled this process")


def _request(root):
    root.mkdir(mode=0o700)
    (root / "trials").mkdir(mode=0o700)
    return core.TrialExecutionRequest(
        evaluation_id="eval-capture",
        kind="suite",
        bot="isolated-bot",
        output=root,
        suite_id="isolated-suite",
        profile="",
        profile_case=None,
        case=EvalCase("sample", "template", "test", "answer"),
        dimension="test",
        target=core.EvaluationTarget(
            "main", "main", "agent_configured", "native", "controlled", "", "fingerprint"
        ),
        attempt=1,
        order=1,
    )


@pytest.mark.parametrize("cancel", [True, False])
def test_supervised_trial_preserves_output_before_judge_cancel_or_timeout(tmp_path, cancel):
    request = _request(tmp_path / "evaluation")
    seen = []
    with ArtifactIntegrityGuard.capture(
        request.output, evaluation_id=request.evaluation_id
    ) as guard:

        def publish(execution):
            payload = {
                "evaluation_id": request.evaluation_id,
                "trial_id": core._trial_id(request),
                "execution": execution,
            }
            guard.publish_observation(payload)
            seen.append(execution)

        with pytest.raises(
            core._TrialExecutionCancelled if cancel else core._TrialExecutionDeadlineExceeded
        ):
            core._execute_supervised_trial(
                request,
                budget=core._TrialExecutionBudget(seconds=3, scope="case"),
                cancel_check=lambda: cancel and bool(seen) and seen[-1].get("phase") == "judging",
                executor=_produce_then_wait,
                observation_callback=publish,
            )
        guard.verify()
    payload = json.loads((request.output / "observation.json").read_text())
    assert payload["execution"]["turns"][0]["final_text"] == "generated answer"
    trial = core._error_trial(request, TimeoutError("judge deadline"))
    assert trial.final_text == "generated answer" and trial.stop_reason == "end_turn"
    assert not (request.output / "result.json").exists()
    assert list((request.output / "trials").iterdir()) == []


def test_capture_updates_by_turn_identity_and_marks_truncation():
    with capture() as result:
        record_turn({"conversation_id": "a", "turn_index": 0, "input": "first"})
        record_turn({"conversation_id": "b", "turn_index": 0, "input": "second"})
        record_turn(
            {"conversation_id": "a", "turn_index": 0, "input": "first", "final_text": "z" * 200000}
        )
        assert len(result["turns"]) == 2
        assert result["state"] == "truncated"
        assert len(result["turns"][0]["final_text"]) == 131072
        assert result["turns"][1]["input"] == "second"


def test_capture_keeps_large_outputs_bounded_and_does_not_promote_an_earlier_turn():
    from chatcopilot.evals.trial_capture import capture_case
    from chatcopilot.evals.models import EvalCaseResult

    @capture_case
    def large():
        return EvalCaseResult(
            "case",
            "suite",
            "passed",
            final_text="answer" * 40000,
            metadata={"tool_result": "result" * 40000},
        )

    result = large()
    assert result.status == "passed" and len(result.final_text) == 131072
    assert result.metadata["execution"]["state"] == "truncated"
    assert len(result.metadata["tool_result"]) == 131072

    @capture_case
    def interrupted():
        record_turn(
            {
                "conversation_id": "actor",
                "turn_index": 0,
                "completed": True,
                "final_text": "earlier reply",
            }
        )
        record_turn(
            {
                "conversation_id": "actor",
                "turn_index": 1,
                "completed": False,
                "input": "new request",
            }
        )
        return EvalCaseResult("case", "suite", "error", error="interrupted")

    result = interrupted()
    assert result.final_text == ""
    assert result.metadata["execution"]["turns"][0]["final_text"] == "earlier reply"


def test_agent_usage_does_not_absorb_judge_usage(tmp_path):
    from chatcopilot.evals.models import EvalCaseResult, JudgeResult

    request = _request(tmp_path / "usage")
    result = EvalCaseResult(
        "sample",
        "isolated-suite",
        "passed",
        judge=JudgeResult(1, 1, True),
        metadata={
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "judge_evidence": {"usage": {"prompt_tokens": 90, "completion_tokens": 20}},
        },
    )
    trial = core._trial_from_case_result(request, result)
    assert trial.usage_totals == {"prompt_tokens": 10, "completion_tokens": 5}
    assert trial.evidence["judge_evidence"]["usage"]["prompt_tokens"] == 90
