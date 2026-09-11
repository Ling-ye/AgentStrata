from dataclasses import replace

import pytest

from chatcopilot.evals.adapters.gaia import judge
from chatcopilot.evals.benchmark_scoring import score_benchmark
from chatcopilot.evals.evaluations import parse_evaluation_request
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.workbench import benchmark_descriptor, benchmark_snapshot, scoring_plan
from chatcopilot.evals.application.insights import benchmark_comparison_keys


def example(answer="42"):
    return EvalCase(case_id="example", input="Return the requested answer.", category="test",
                    expected_behavior="Provide the exact answer.", metadata={"answer": answer})


@pytest.mark.parametrize("expected,actual,passed", [
    ("42", "42", True), ("42", "Answer: 42", False), ("1000", "$1,000", True),
    ("The Blue Whale", "thebluewhale.", True), ("The Blue Whale", "blue whale", False),
    ("A, 2; C", "a;2,c", True), ("A, B", "B,A", False), ("A, B", "A", False),
    ("New York", "New\nYork", True), ("42", "bad answer\n42", False),
])
def test_gaia_preserves_official_answer_semantics(expected, actual, passed):
    assert judge(example(expected), actual).passed is passed


def test_native_failure_survives_high_quality_score(deepeval_judge):
    result, evidence = score_benchmark("gaia", example(), "wrong", lambda: judge(example(), "wrong"),
                                       options={"scoring_mode": "native_geval"}, judge_model=deepeval_judge)
    assert not result.passed
    assert evidence["native_result"]["passed"] is False
    assert evidence["metrics"][1]["passed"] is True
    assert evidence["metrics"][1]["score"] == .9


def test_judge_error_does_not_destroy_native_result(deepeval_judge):
    deepeval_judge.failure = TimeoutError("controlled judge timeout")
    result, evidence = score_benchmark("gaia", example(), "42", lambda: judge(example(), "42"),
                                       options={"scoring_mode": "native_geval"}, judge_model=deepeval_judge)
    assert result.passed
    assert evidence["metrics"][0]["passed"] is True
    assert evidence["metrics"][1]["score"] is None
    assert "timeout" in evidence["metrics"][1]["error"]


def test_native_only_does_not_create_a_judge(monkeypatch):
    monkeypatch.setattr("chatcopilot.evals.deepeval_engine._model", lambda *_: pytest.fail("judge created"))
    result, evidence = score_benchmark("gaia", example(), "42", lambda: judge(example(), "42"), options={"scoring_mode": "native"})
    assert result.passed and evidence["quality_applicable"] is False


def test_public_benchmark_cannot_replace_native_scoring(deepeval_judge):
    with pytest.raises(ValueError, match="unsupported scoring mode"):
        score_benchmark("gaia", example(), "42", lambda: pytest.fail("native scorer called"),
                        options={"scoring_mode": "geval"}, judge_model=deepeval_judge)


def test_request_freezes_declared_scoring_and_rejects_flag_conflict(monkeypatch):
    monkeypatch.setattr("chatcopilot.evals.evaluations.get_cases", lambda *args, **kwargs: (example(),))
    request = parse_evaluation_request({"kind": "suite", "suite": "bfcl", "dry_run": True,
                                        "options": {"scoring_mode": "native_geval", "quality_rubric": "task"}})
    assert request.options["scoring_mode"] == "native_geval"
    with pytest.raises(ValueError, match="conflicts"):
        parse_evaluation_request({"kind": "suite", "suite": "gaia", "dry_run": True,
                                  "llm_judge": True, "options": {"scoring_mode": "native"}})


def test_case_content_and_judge_changes_have_metric_specific_comparison_keys(deepeval_judge, monkeypatch):
    manifest = get_manifest("gaia")
    benchmark = benchmark_snapshot(manifest, [example()], {"scoring_mode": "native_geval"})
    result = {"config_snapshot": {"benchmark": benchmark, "definition_snapshot": {}}}
    before = benchmark_comparison_keys({}, result)
    benchmark["scoring"] = {**benchmark["scoring"], "judge": {"model": "another-judge"}}
    after = benchmark_comparison_keys({}, result)
    assert before["pass_rate"] == after["pass_rate"]
    assert before["quality"] != after["quality"]
    benchmark["scoring"]["native"] = False
    custom = benchmark_comparison_keys({}, result)
    benchmark["scoring"]["judge"] = {"model": "third-judge"}
    assert custom["pass_rate"] != benchmark_comparison_keys({}, result)["pass_rate"]
    benchmark["scoring"]["native"] = True
    benchmark["case_set_hash"] = benchmark_snapshot(manifest, [replace(example(), input="different")], {})["case_set_hash"]
    assert benchmark_comparison_keys({}, result)["pass_rate"] != after["pass_rate"]
    assert benchmark_comparison_keys({}, {}) == {}


def test_descriptor_does_not_present_bfcl_as_full_agent_or_official_score():
    descriptor = benchmark_descriptor(get_manifest("bfcl"), [example()])
    assert "direct_llm" in descriptor["target_scope"]
    assert "部分" in descriptor["coverage"]
    assert "项目适配器" in descriptor["native_method"]
    assert scoring_plan(get_manifest("gaia"), {"scoring_mode": "native"})["judge"] is None


def test_native_metric_respects_passed_independently_of_numeric_score():
    result, evidence = score_benchmark("bfcl", example(), "42",
        lambda: JudgeResult(score=.5, max_score=1, passed=True), options={"scoring_mode": "native"})
    assert result.passed and evidence["native_result"]["score"] == .5


def test_benchmark_sdk_context_disables_ambient_dotenv(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    from chatcopilot.evals.deepeval_engine import _local_sdk

    env_file = tmp_path / ".env"
    env_file.write_text("WORKBENCH_AMBIENT_VALUE=unwanted\n")
    monkeypatch.delenv("WORKBENCH_AMBIENT_VALUE", raising=False)
    import os

    with _local_sdk():
        load_dotenv(env_file)
        assert "WORKBENCH_AMBIENT_VALUE" not in os.environ


def test_swebench_trends_require_observed_image_identity():
    result = {"config_snapshot": {"benchmark": {"schema": "evaluation-workbench/v1",
        "suite_id": "swe-bench-verified", "case_set_hash": "case-hash", "scoring": {"native": True}}},
        "trials": [{"case_id": "case-1", "evidence": {"image_id": "image-a"}}]}
    original = benchmark_comparison_keys({}, result)
    result["trials"][0]["evidence"]["image_id"] = "image-b"
    assert original["pass_rate"] != benchmark_comparison_keys({}, result)["pass_rate"]
    result["trials"][0]["evidence"] = {}
    assert benchmark_comparison_keys({}, result) == {}


def test_qq_synthetic_snapshot_reports_actual_checker():
    plan = scoring_plan(get_manifest("agentstrata-qq-message-flow-v1"), {})
    assert plan["framework"] == "AgentStrata 链路检查器"
    assert plan["framework_version"] == ""


def test_catalog_subjects_and_snapshots_preserve_execution_boundaries():
    from chatcopilot.evals.catalog import list_suite_manifests

    expected = {
        "bfcl": "model", "project-business-v1": "agent", "agentstrata-capabilities-v1": "agent",
        "gaia": "agent", "ifeval": "agent", "agentbench-fc": "agent", "swe-bench-verified": "agent",
        "webarena": "agent", "agentstrata-qq-message-flow-v1": "system", "agentstrata-canary-self-update-v1": "system",
    }
    manifests = {item.suite_id: item for item in list_suite_manifests()}
    assert {key: value.subject_type for key, value in manifests.items()} == expected
    for ident, manifest in manifests.items():
        assert manifest.capability_tags
        snapshot = benchmark_snapshot(manifest, [], {})
        assert snapshot["subject_type"] == expected[ident]
        assert snapshot["capability_tags"] == list(manifest.capability_tags)
    assert manifests["bfcl"].driver_id == "direct_llm"
    assert manifests["ifeval"].driver_id == "agent_configured"
    assert "Legacy" in manifests["agentstrata-qq-message-flow-v1"].coverage
    for ident in ("webarena", "agentstrata-canary-self-update-v1"):
        assert manifests[ident].status == "planned"
        assert not manifests[ident].plugin_id


def test_subject_change_separates_trends_without_backfilling_legacy():
    snapshot = benchmark_snapshot(get_manifest("ifeval"), [example()], {})
    result = {"config_snapshot": {"benchmark": snapshot}}
    original = benchmark_comparison_keys({}, result)
    snapshot["subject_type"] = "model"
    assert original != benchmark_comparison_keys({}, result)
    del snapshot["subject_type"]
    legacy = benchmark_comparison_keys({}, result)
    assert original != legacy
    assert "subject_type" not in snapshot


def test_case_tags_survive_definition_business_and_catalog_projections():
    from chatcopilot.evals.application.catalog import get_case_descriptor, list_case_summaries

    for suite_id in ("agentstrata-capabilities-v1", "project-business-v1", "agentstrata-qq-message-flow-v1"):
        cases = list_case_summaries(suite_id)
        multiple = next(case for case in cases if len(case["capability_tags"]) > 1)
        assert get_case_descriptor(suite_id, multiple["case_id"])["capability_tags"] == multiple["capability_tags"]
