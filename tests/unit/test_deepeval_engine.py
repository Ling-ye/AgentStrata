from dataclasses import replace
import os

import pytest

from chatcopilot.evals import deepeval_engine as engine
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.registry import get_manifest


def case():
    original = load_case_definitions(get_manifest("agentstrata-capabilities-v1"))[0]
    return replace(
        original,
        quality={
            "enabled": True,
            "threshold": 0.7,
            "expected": "Return the requested JSON.",
            "steps": ["Check actual_output against expected_output and input."],
        },
    )


def observation(text='{"name":"sample","value":1}', *, turns=1):
    return TrialObservation(
        final_text=text,
        stop_reason="end_turn",
        evidence=tuple(
            {
                "kind": "agent_turn_result",
                "conversation_id": "isolated",
                "input": f"actual request {index}",
                "final_text": text,
            }
            for index in range(turns)
        ),
    )


@pytest.mark.parametrize("turns", [1, 2])
def test_actual_sdk_runs_fact_and_quality_metrics(deepeval_judge, turns):
    result, detail = engine.score(case(), observation(turns=turns))
    assert result.passed
    assert detail["judge_kind"] == "deepeval"
    assert [m["kind"] for m in detail["metrics"]] == ["deterministic", "quality"]
    assert detail["metrics"][1]["score"] == 0.9
    assert "actual request" in deepeval_judge.prompts[-1]
    assert (
        "Conversational" in detail["metrics"][1]["name"]
        if turns == 2
        else "Conversational" not in detail["metrics"][1]["name"]
    )


def test_quality_cannot_override_missing_execution_facts(deepeval_judge):
    result, detail = engine.score(case(), observation("plain text"))
    assert not result.passed
    assert detail["metrics"][1]["passed"] is True


def test_low_quality_and_judge_error_are_distinct(deepeval_judge):
    deepeval_judge.value = 3
    result, detail = engine.score(case(), observation())
    assert not result.passed and not detail["error"]
    assert detail["passed"] is False and detail["facts_passed"] is True
    assert detail["metrics"][0]["passed"] is True
    deepeval_judge.failure = TimeoutError("controlled timeout")
    result, detail = engine.score(case(), observation())
    assert not result.passed and detail["error"]
    assert "timeout" in detail["error"]


def test_preflight_rejects_missing_judge_without_importing_sdk(monkeypatch):
    for key in ("MODEL", "BASE_URL", "API_KEY"):
        monkeypatch.delenv("CHATCOPILOT_EVALUATION_JUDGE_" + key, raising=False)
    with pytest.raises(ValueError, match="独立评分模型"):
        engine.preflight([case()])


def test_all_agent_cases_have_explicit_quality_policy():
    cases = load_case_definitions(get_manifest("agentstrata-capabilities-v1"))
    assert len(cases) == 25
    assert all("enabled" in engine.quality_policy(c) for c in cases)


def test_sdk_environment_restored_and_does_not_read_dotenv(tmp_path, monkeypatch, deepeval_judge):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EVALUATION_UNEXPECTED_DOTENV=loaded\n")
    monkeypatch.setenv("CONFIDENT_API_KEY", "not-used-test-key")
    before = dict(os.environ)
    engine.score(case(), observation())
    assert dict(os.environ) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_fresh_sdk_process_does_not_connect_or_load_ambient_dotenv(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    (tmp_path / ".env").write_text("EVALUATION_UNEXPECTED_DOTENV=loaded\n")
    script = """
import os, socket
connections = []
def rejected(self, address):
    connections.append(address)
    raise AssertionError("unexpected SDK network connection")
socket.socket.connect = rejected
from chatcopilot.evals.deepeval_engine import score
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.models import TrialObservation
case = load_case_definitions(get_manifest("agentstrata-capabilities-v1"))[0]
result, evidence = score(case, TrialObservation(final_text='{"name":"test","value":1}', stop_reason="end_turn"))
assert result.passed, evidence.get("error")
assert not connections
assert "EVALUATION_UNEXPECTED_DOTENV" not in os.environ
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "CONFIDENT_API_KEY": "unused-controlled-key",
        "ENV_DIR_PATH": str(tmp_path),
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_framework_metric_is_an_error(monkeypatch, deepeval_judge):
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module("deepeval.evaluate")
    monkeypatch.setattr(
        module,
        "evaluate",
        lambda **kwargs: SimpleNamespace(test_results=[SimpleNamespace(metrics_data=None)]),
    )
    result, detail = engine.score(case(), observation())
    assert not result.passed
    assert "no complete metric" in detail["error"]


def test_real_judge_adapter_builds_prompt_plan_and_scores_with_host_client(monkeypatch):
    import json

    from chatcopilot.core.llm_client import ChatResult, LLMClient

    for key, value in {
        "MODEL": "configured-judge",
        "BASE_URL": "https://judge.example.test/v1",
        "API_KEY": "controlled-judge-key",
    }.items():
        monkeypatch.setenv("CHATCOPILOT_EVALUATION_JUDGE_" + key, value)
    requests = []

    def chat(client, **kwargs):
        requests.append((client.config, kwargs))
        return ChatResult(
            content='{"score":9,"reason":"The output matches the expected JSON."}',
            usage={"prompt_tokens": 12, "completion_tokens": 8},
        )

    monkeypatch.setattr(LLMClient, "chat", chat)
    result, details = engine.score(case(), observation())
    assert result.passed, details["error"]
    assert details["calls"] == 1
    assert details["usage"] == {"prompt_tokens": 12, "completion_tokens": 8}
    config, request = requests[0]
    assert config.model == "configured-judge"
    assert request["max_retries"] == 0
    assert request["timeout"] == 60
    system = json.loads(request["messages"][0]["content"])
    assert "evaluation judge" in system["host_policy"]
    content = "\n".join(message["content"] for message in request["messages"])
    assert "AgentStrata evaluation judge." in content
    assert "actual request" in content
    assert "controlled-judge-key" not in content
