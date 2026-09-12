"""Official scorer boundaries, model evidence, and unavailable data behavior."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from chatcopilot.evals import bfcl_official as official
from chatcopilot.evals.adapters import bfcl, ifeval
from chatcopilot.evals.model_io import invoke, output_preview
from chatcopilot.evals.models import EvalCase
from chatcopilot.evals.trial_capture import capture


def case_for(*, language="Python", category="simple_python", typ="float", candidates=None):
    return EvalCase(case_id="official-boundary", input="Find the measurement.", category=category, expected_behavior="call", metadata={
        "language": language, "bfcl_category": category,
        "functions": [{"name": "measure.value", "description": "Measure a value", "parameters": {
            "type": "dict", "properties": {"x": {"type": typ, "description": "value"}, "unit": {"type": "string", "description": "unit"}}, "required": ["x"]}}],
        "possible_answer": [{"measure.value": {"x": candidates or [2.0, 3.0], "unit": ["", "m"]}}],
    })


def call(x=2.0, **extra):
    return {"id": "call-synthetic", "function": {"name": "measure_value", "arguments": json.dumps({"x": x, **extra})}}


@pytest.mark.parametrize("calls,expected", [([call()], True), ([call(3.0, unit="m")], True),
    ([call(20.0)], False), ([call(), call()], False), ([call(extra=1)], False), ([], False)])
def test_official_candidates_optional_and_call_count(calls, expected):
    assert official.judge(case_for(), calls).passed is expected


@pytest.mark.parametrize("language,category,typ,candidates,value", [
    ("Java", "simple_java", "integer", [2], "2"),
    ("JavaScript", "simple_javascript", "float", [2.0], "2.0"),
])
def test_official_language_types(language, category, typ, candidates, value):
    case = case_for(language=language, category=category, typ=typ, candidates=candidates)
    assert official.tools_for(case.metadata["functions"], category=category)[0]["function"]["name"] == "measure_value"
    assert official.judge(case, [call(value)]).passed
    assert not official.judge(case, [call("incorrect")]).passed


def test_parallel_order_and_extra_calls():
    case = case_for(category="parallel")
    case.metadata["possible_answer"].append({"measure.value": {"x": [7.0], "unit": [""]}})
    assert official.judge(case, [call(7.0), call(2.0)]).passed
    assert not official.judge(case, [call(7.0)]).passed
    assert not official.judge(case, [call(7.0), call(2.0), call(2.0)]).passed


@pytest.mark.parametrize("category,empty,nonempty", [("irrelevance", True, False), ("live_irrelevance", True, False), ("live_relevance", False, True)])
def test_official_relevance_rules(category, empty, nonempty):
    case = case_for(category=category)
    assert official.judge(case, []).passed is empty
    assert official.judge(case, [call()]).passed is nonempty
    # Upstream relevance treats an undecodable call as absence of a valid call.
    assert official.judge(case, [{"function": {"name": "measure_value", "arguments": "{"}}]).passed is empty


def test_missing_answers_and_schema_collisions_are_errors():
    case = case_for()
    case.metadata.pop("possible_answer")
    with pytest.raises(ValueError, match="candidate answers"):
        official.judge(case, [])
    functions = deepcopy(case.metadata["functions"])
    functions.append({**functions[0], "name": "measure_value"})
    with pytest.raises(ValueError, match="collide"):
        official.tools_for(functions)


def test_complete_v4_files_and_answer_ids_required(tmp_path):
    from test_eval_console import _write_bfcl_official_cache
    _write_bfcl_official_cache(tmp_path)
    assert len(official.rows_to_cases(tmp_path)) == 520
    answer = tmp_path / "possible_answer" / official.filename("simple_python")
    answer.write_text(answer.read_text().splitlines()[0] + "\n")
    with pytest.raises(ValueError, match="IDs differ"):
        official.rows_to_cases(tmp_path)
    answer.unlink()
    with pytest.raises(ValueError, match="missing regular"):
        official.rows_to_cases(tmp_path)


def test_default_missing_data_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_EVALS_DATA_DIR", str(tmp_path))
    for key in ("CHATCOPILOT_BFCL_DATA_DIR", "CHATCOPILOT_BFCL_CASE_PROFILE", "CHATCOPILOT_IFEVAL_DATA_PATH", "CHATCOPILOT_IFEVAL_CASE_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    assert bfcl.load_cases() == ifeval.load_cases() == ()
    monkeypatch.setenv("CHATCOPILOT_BFCL_CASE_PROFILE", "smoke")
    monkeypatch.setenv("CHATCOPILOT_IFEVAL_CASE_PROFILE", "smoke")
    assert len(bfcl.load_cases()) == 5
    assert len(ifeval.load_cases()) == 8


@pytest.mark.parametrize("text,calls,kind", [("answer", [], "text"), ("", [call()], "tool_calls"), ("answer", [call()], "mixed"), ("", [], "empty")])
def test_recorded_output_kinds(text, calls, kind):
    row = {"final_text": text, "evidence": {"model_response": {"content": text, "tool_calls": calls, "finish_reason": "stop"}}}
    assert output_preview(row)["kind"] == kind


def test_historical_projection_and_bounds_never_mutate():
    row = {"final_text": "", "evidence": {"tool_calls": [call()]}}
    before = deepcopy(row)
    assert output_preview(row)["kind"] == "tool_calls"
    assert row == before
    assert output_preview({})["kind"] == "missing"
    row = {"final_text": "x" * 1000, "evidence": {"execution": {"state": "truncated"}, "tool_calls": [call("x" * 1000)] * 10}}
    preview = output_preview(row)
    assert preview["truncated"] and len(preview["text"]) == 400
    assert preview["call_count"] == 10 and len(preview["calls"]) == 3
    assert len(preview["calls"][0]["arguments"]) == 160


@pytest.mark.parametrize("fail", [False, True])
def test_model_capture_actual_messages_and_client_close(monkeypatch, fail):
    state = {}
    class Client:
        def __init__(self, _config): pass
        def chat(self, **kwargs):
            state.update(kwargs)
            if fail:
                raise RuntimeError("synthetic transport error")
            return SimpleNamespace(content="", tool_calls=[call()], finish_reason="tool_calls", usage={"total_tokens": 3})
        def close(self): state["closed"] = True
    monkeypatch.setattr("chatcopilot.core.llm_client.LLMClient", Client)
    case = case_for()
    messages = [{"role": "system", "content": "benchmark context"}, {"role": "user", "content": "original task"}]
    with capture() as observed:
        if fail:
            with pytest.raises(RuntimeError):
                invoke(case, chat_config=SimpleNamespace(llm=SimpleNamespace(model="synthetic")), messages=messages, tools=official.tools_for(case.metadata["functions"]))
        else:
            result = invoke(case, chat_config=SimpleNamespace(llm=SimpleNamespace(model="synthetic")), messages=messages, tools=official.tools_for(case.metadata["functions"]))
            assert result["metadata"]["model_response"]["finish_reason"] == "tool_calls"
            assert result["metadata"]["model_response"]["tool_calls"] == [call()]
    assert state["closed"]
    assert state["messages"][-1] == messages[-1]
    assert state["messages"][-2] == {"role": "user", "content": "Benchmark context:\nbenchmark context"}
    assert observed["turns"][0]["model_request"]["messages"] == state["messages"]


def test_judge_failure_keeps_model_response(monkeypatch):
    from chatcopilot.evals.plugins.bfcl import PLUGIN
    from tests.evaluation_fixtures import run_direct_cases
    case = bfcl._smoke_cases()[0]
    def execute(*args, **kwargs):
        from chatcopilot.evals.trial_capture import record_turn
        response = {"content": "retained", "tool_calls": [call()], "finish_reason": "tool_calls", "usage": {}}
        request = {"messages": [{"role": "user", "content": "actual"}], "tools": []}
        record_turn({"conversation_id": case.case_id, "turn_index": 0, "model_request": request, "model_response": response, "completed": True})
        return {"final_text": "retained", "tool_calls": [call()], "metadata": {"model_request": request, "model_response": response}}
    def broken(*args): raise ValueError("synthetic checker failure")
    monkeypatch.setattr("chatcopilot.evals.case_drivers._load_bot_config", lambda bot: object())
    result = run_direct_cases("bfcl", replace(PLUGIN, execute_model=execute, judge=broken), (case,), bot="synthetic")[0]
    assert result.status == "error" and result.final_text == "retained"
    assert result.metadata["model_response"]["tool_calls"] == [call()]
    assert result.error.code == "judge_error"
