"""Pinned checker parity, fail-closed data and no-Agent model execution."""

from types import SimpleNamespace
import json

import pytest

from chatcopilot.evals.adapters import ifeval
from chatcopilot.evals.ifeval_official import build_checker, check_instructions
from chatcopilot.evals.registry import get_cases, get_manifest
from chatcopilot.evals.plugins.ifeval import _execute_trial
from chatcopilot.evals.trial_capture import capture


@pytest.mark.parametrize(
    "ident,args,output",
    [
        (
            "keywords:frequency",
            {"keyword": "red", "frequency": 2, "relation": "less than"},
            "red red",
        ),
        (
            "keywords:frequency",
            {"keyword": "red", "frequency": 2, "relation": "at least"},
            "red red",
        ),
        ("detectable_format:json_format", {}, "[1, 2]"),
        ("detectable_format:number_bullet_lists", {"num_bullets": 2}, "* one\n* two"),
        ("punctuation:no_comma", {}, "hello, world"),
        ("startend:end_checker", {"end_phrase": "the end"}, "story\nTHE END"),
    ],
)
def test_strict_checker_is_the_pinned_official_implementation(ident, args, output):
    checker = build_checker(ident, args)
    result = check_instructions([ident], [args], output)[0]
    assert result["passed"] is bool(checker.check_following(output))
    if args.get("relation") == "less than":
        assert not result["passed"]


def test_mixed_supported_and_unsupported_constraints_are_never_dropped(tmp_path):
    p = tmp_path / "questions.jsonl"
    p.write_text(
        json.dumps(
            {
                "key": 1,
                "prompt": "test",
                "instruction_id_list": ["punctuation:no_comma", "not_supported"],
                "kwargs": [{}, {}],
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="Unsupported"):
        ifeval._load_jsonl_cases(p)
    with pytest.raises(ValueError, match="parameter"):
        check_instructions(["keywords:frequency"], [{"keyword": "red"}], "red")
    with pytest.raises(ValueError, match="count mismatch"):
        check_instructions(["punctuation:no_comma"], [], "hello")


def test_direct_call_uses_neither_agent_nor_bot_persona_and_closes_client(monkeypatch):
    import chatcopilot.core.llm_client as client_module
    import chatcopilot.application.agent_runtime as assembly

    monkeypatch.setenv("CHATCOPILOT_IFEVAL_CASE_PROFILE", "smoke")
    requests = []
    closed = []

    class Client:
        def __init__(self, config):
            pass

        def chat(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(content="A calm journey unfolds", tool_calls=[], finish_reason="stop", usage={"input_tokens": 3})

        def close(self):
            closed.append(True)

    case = get_cases("ifeval")[0]

    monkeypatch.setattr(client_module, "LLMClient", Client)
    monkeypatch.setattr(
        assembly,
        "assemble_agent_runtime",
        lambda *a, **k: pytest.fail("Agent runtime must not be created"),
    )
    with capture():
        result = _execute_trial(
            case, chat_config=SimpleNamespace(llm=SimpleNamespace(model="fixture-model"))
        )
    assert requests[0]["tools"] is None and len(requests) == 1 and closed == [True]
    assert requests[0]["messages"][-1] == {"role": "user", "content": case.input}
    assert result["metadata"]["agent_runtime_exercised"] is False
    assert result["metadata"]["model_request"]["messages"] == requests[0]["messages"]
    assert get_manifest("ifeval").driver_id == "direct_llm"
    assert len(get_cases("ifeval")) == 8


def test_language_failure_is_a_scoring_error(monkeypatch):
    from chatcopilot.evals import ifeval_language

    monkeypatch.setattr(
        ifeval_language, "detect", lambda _: (_ for _ in ()).throw(ValueError("detection failed"))
    )
    with pytest.raises(ValueError, match="detection failed"):
        check_instructions(["change_case:english_lowercase"], [{}], "hello there")


def test_blank_response_never_passes_vacuously():
    assert not check_instructions(["punctuation:no_comma"], [{}], "")[0]["passed"]
