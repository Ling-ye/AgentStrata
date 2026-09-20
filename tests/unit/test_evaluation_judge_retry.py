"""Real Judge/DeepEval/SDK paths with controlled HTTP transport and virtual time."""

from contextlib import contextmanager
from dataclasses import replace
import errno
import json
from types import SimpleNamespace

import httpx
from openai import OpenAI
from pydantic import BaseModel
import pytest

from chatcopilot.core.llm_client import LLMClient
from chatcopilot.evals import deepeval_engine as engine
from chatcopilot.evals.agent_case import SCHEMA, SUITE, case_identity, evaluation_cases, validate_case
from chatcopilot.evals.models import TrialObservation, to_jsonable


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(responses=[], requests=[], sleeps=[], now=0.0)
    for key, value in {"MODEL": "controlled-judge", "BASE_URL": "https://private-judge.test/v1",
                       "API_KEY": "private-judge-key"}.items():
        monkeypatch.setenv("CHATCOPILOT_EVALUATION_JUDGE_" + key, value)
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_JUDGE_REASONING_EFFORT", raising=False)

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))

    def respond(request):
        state.requests.append(json.loads(request.content))
        state.now += 0.1
        response = state.responses.pop(0)
        private = "private-judge-key https://private-judge.test/v1 raw-provider-body"
        if response == "connection":
            try:
                raise OSError(errno.ECONNREFUSED, private)
            except OSError as exc:
                raise httpx.ConnectError(private, request=request) from exc
        if response == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if isinstance(response, int):
            return httpx.Response(response, json={"error": {"message": private}})
        return httpx.Response(200, json={"id": "controlled", "object": "chat.completion",
            "model": "controlled-judge", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": response}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})

    monkeypatch.setattr(LLMClient, "_build_client", lambda self: OpenAI(
        api_key=self.config.api_key, base_url=self.config.base_url, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond))))
    return state


class _Score(BaseModel):
    score: int
    reason: str


def _response(score=9):
    return json.dumps({"score": score, "reason": "controlled judgment"})


@contextmanager
def _judge():
    with engine._local_sdk():
        model = engine._model(engine.JudgeConfig.from_environment())
        try:
            yield model
        finally:
            model.model.close()


def _assert_private_values_absent(value):
    text = json.dumps(value)
    for private in ("private-judge-key", "private-judge.test", "raw-provider-body"):
        assert private not in text


@pytest.mark.parametrize("failure", ["connection", "timeout", 408, 429, 500, 502, 503, 504])
def test_transient_failure_retries_identical_request_only(transport, failure):
    transport.responses = [failure, _response()]
    with _judge() as model:
        result = model.generate("Score the frozen observation", schema=_Score)
    assert result.score == 9
    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert transport.sleeps == [0.8]
    assert model.calls == 1
    assert model.usage["prompt_tokens"] == 12
    assert [a["outcome"] for a in model.judge_attempts] == ["error", "succeeded"]
    assert all(a["elapsed_seconds"] == pytest.approx(0.1) for a in model.judge_attempts)
    assert model.judge_attempts[0]["retryable"] is True
    _assert_private_values_absent(model.judge_attempts)


def test_exhausted_connection_errors_are_bounded_and_diagnostic(transport):
    transport.responses = ["connection"] * 3
    with _judge() as model, pytest.raises(RuntimeError) as failed:
        model.generate("Score the frozen observation", schema=_Score)
    assert len(transport.requests) == 3
    assert transport.sleeps == [0.8, 1.6]
    assert model.calls == 0 and model.usage == {}
    assert [a["attempt"] for a in model.judge_attempts] == [1, 2, 3]
    diagnostic = model.judge_attempts[-1]
    assert diagnostic["error_type"] == "APIConnectionError"
    assert diagnostic["causes"] == [
        {"error_type": "APIConnectionError"}, {"error_type": "ConnectError"},
        {"error_type": "ConnectionRefusedError", "errno": errno.ECONNREFUSED}]
    assert "attempts=3" in str(failed.value)
    assert "ConnectionRefusedError" in str(failed.value)
    _assert_private_values_absent([str(failed.value), model.judge_attempts])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nonretryable_http_errors_are_sanitized(transport, status):
    transport.responses = [status]
    with _judge() as model, pytest.raises(RuntimeError) as failed:
        model.generate("Score the frozen observation", schema=_Score)
    assert len(transport.requests) == 1 and transport.sleeps == []
    assert model.judge_attempts[0]["http_status"] == status
    assert model.judge_attempts[0]["retryable"] is False
    assert f"HTTP {status}" in str(failed.value)
    _assert_private_values_absent([str(failed.value), model.judge_attempts])


@pytest.mark.parametrize("content", ["raw-provider-body private-judge-key", '{"score":"private-judge-key"}'])
def test_invalid_json_or_schema_is_not_retried_or_logged(transport, content):
    transport.responses = [content]
    with _judge() as model, pytest.raises(ValueError, match="judge response invalid") as failed:
        model.generate("Score the frozen observation", schema=_Score)
    assert len(transport.requests) == 1 and transport.sleeps == []
    assert model.calls == 1 and model.usage["prompt_tokens"] == 12
    assert model.judge_attempts[0]["outcome"] == "invalid_response"
    _assert_private_values_absent([str(failed.value), model.judge_attempts])


@pytest.mark.parametrize("exhausted", [False, True])
def test_direct_agent_scoring_preserves_facts_and_attempt_evidence(transport, exhausted):
    from chatcopilot.evals.manifest import load_case_definitions
    from chatcopilot.evals.registry import get_manifest

    case = replace(load_case_definitions(get_manifest("agentstrata-capabilities-v1"))[0],
        quality={"enabled": True, "threshold": 0.7, "expected": "Return JSON.",
                 "steps": ["Check output against the requested JSON."]})
    transport.responses = ["connection"] * 3 if exhausted else ["connection", _response()]
    result, evidence = engine.score(case, TrialObservation(
        final_text='{"name":"sample","value":1}', stop_reason="end_turn"))
    assert evidence["metrics"][0]["passed"] is True
    assert result.passed is not exhausted
    assert len(evidence["judge_attempts"]) == (3 if exhausted else 2)
    if exhausted:
        assert evidence["metrics"][1]["score"] is None
        assert "APIConnectionError" in evidence["error"] and "attempts=3" in evidence["error"]
    else:
        assert not evidence["error"]
    _assert_private_values_absent(evidence)


def _frozen_case():
    declaration = validate_case({"schema": SCHEMA, "title": "Read fixture",
        "input": "Read note.txt", "expected_behavior": "Report the actual value",
        "allowed_tools": ["read_text_head"], "semantic": True,
        "fixtures": {"note.txt": "sample-value"},
        "assertions": [{"kind": "tool_called", "name": "read_text_head"},
                       {"kind": "final_contains", "value": "sample-value"}]})
    return evaluation_cases({"snapshot_id": case_identity(declaration), "case": declaration})[0]


@pytest.mark.parametrize("outcome", ["recovered", "exhausted", "low_score", "invalid_json"])
def test_frozen_trial_preserves_execution_and_never_reruns_agent(transport, monkeypatch, tmp_path, outcome):
    from chatcopilot.evals import frozen_agent_runtime
    from chatcopilot.evals.evaluations import _trial_from_case_result
    from chatcopilot.evals.result_codec import trial_from_dict
    from chatcopilot.evals.trial_runner import run_case
    from test_evaluation_execution_capture import _request

    transport.responses = {
        "recovered": ["connection", _response()], "exhausted": ["connection"] * 3,
        "low_score": [_response(3)], "invalid_json": ["raw-provider-body private-judge-key"],
    }[outcome]
    executions = []

    def run(*args, **kwargs):
        executions.append("agent-and-tool")
        return TrialObservation(final_text="sample-value", stop_reason="end_turn",
            tool_calls=({"name": "read_text_head", "arguments": {"path": "note.txt"},
                         "ok": True, "result": "sample-value"},))

    monkeypatch.setattr(frozen_agent_runtime, "run", run)
    case = _frozen_case()
    result = run_case(case, suite_id=SUITE, bot="controlled", workspace_root=tmp_path, options={})
    assert executions == ["agent-and-tool"]
    request = replace(_request(tmp_path / "result"), case=case, suite_id=SUITE)
    trial = trial_from_dict(to_jsonable(_trial_from_case_result(request, result)))
    assert trial.final_text == "sample-value"
    evidence = trial.assessment.evidence
    assert evidence["assertions"][0]["passed"] is True
    assert evidence["native_result"]["passed"] is True
    assert len(trial.execution.metadata["tool_calls"]) == 1
    if outcome in {"exhausted", "invalid_json"}:
        assert trial.outcome == "error" and trial.error.code == "judge_error"
        assert trial.score is None and trial.assessment.judge is None
        assert evidence["metrics"][1]["score"] is None
        assert len(transport.requests) == (3 if outcome == "exhausted" else 1)
        if outcome == "exhausted":
            assert "APIConnectionError" in trial.error.message and "attempts=3" in trial.error.message
    else:
        assert trial.outcome == ("passed" if outcome == "recovered" else "failed")
        assert trial.error is None
        assert len(transport.requests) == (2 if outcome == "recovered" else 1)
    assert len(evidence["judge_attempts"]) == len(transport.requests)
    _assert_private_values_absent(to_jsonable(trial))
