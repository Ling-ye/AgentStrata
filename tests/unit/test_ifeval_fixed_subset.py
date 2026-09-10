from dataclasses import replace
import pytest
from chatcopilot.evals.ifeval_subset import (
    check_instructions,
    validate_fixed,
    MetricCollectionError,
    KEYS,
)
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.deepeval_engine import score

OUTPUTS = {
    1001: "Visit Tokyo and then Kyoto.",
    1019: "are the two young boys playing with toy guns and horns?",
    102: "* First\n* Second\n* Third",
    1075: '{"product":"comfortable diaper"}',
    1128: "Neutral. Is there anything else I can help with?",
    1393: "synonyms synonyms synonyms",
    1531: "Our research project includes atlantis and constable.",
    1999: "THE SECOND WORLD WAR WAS A GLOBAL CONFLICT THAT INVOLVED MANY COUNTRIES AND CHANGED THE WORLD.",
}


@pytest.mark.parametrize("key", KEYS)
def test_fixed_original_constraints_through_deepeval(key):
    definition = next(
        c
        for c in load_case_definitions(get_manifest("agentstrata-capabilities-v1"))
        if c.case_id == f"ifeval-fixed-{key}"
    )
    validate_fixed(definition.assertions[0].arguments)
    observation = TrialObservation(final_text=OUTPUTS[key], stop_reason="end_turn")
    passed, evidence = score(definition, observation)
    assert passed.passed, evidence
    assert evidence["judge_kind"] == "deepeval"
    assert evidence["quality_applicable"] is False
    negative = "Invalid, response without the requested constraints."
    if key == 1075:
        negative = "{ broken json"
    assert not score(definition, replace(observation, final_text=negative))[0].passed


def test_official_strict_json_fences_and_end_normalization():
    assert check_instructions(["detectable_format:json_format"], [{}], "```json\n[1,2]\n```")[0][
        "passed"
    ]
    assert check_instructions(["startend:end_checker"], [{"end_phrase": "Goodbye."}], '"GOODBYE."')[
        0
    ]["passed"]
    assert not check_instructions(["punctuation:no_comma"], [{}], "a,b")[0]["passed"]


@pytest.mark.parametrize(
    "ids,args",
    [
        (["unknown:instruction"], [{}]),
        (["keywords:frequency"], [{}]),
        (["keywords:frequency"], [{"keyword": "a", "frequency": 2, "relation": "approximately"}]),
        (["punctuation:no_comma"], []),
        ([], []),
    ],
)
def test_no_dropped_or_default_passing_constraints(ids, args):
    with pytest.raises(MetricCollectionError):
        check_instructions(ids, args, "whatever")


def test_detector_failure_is_grading_error(monkeypatch):
    from chatcopilot.evals import ifeval_subset

    def broken(_value):
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(ifeval_subset, "_english", broken)
    definition = next(
        c
        for c in load_case_definitions(get_manifest("agentstrata-capabilities-v1"))
        if c.case_id == "ifeval-fixed-1019"
    )
    passed, evidence = score(
        definition, TrialObservation(final_text=OUTPUTS[1019], stop_reason="end_turn")
    )
    assert not passed.passed
    assert evidence["error"]
    assert evidence["metrics"][0]["error"]
