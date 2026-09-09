from __future__ import annotations

import copy
import json
import subprocess
from unittest.mock import patch

import pytest

from chatcopilot.evals.application import EvaluationApplication
from chatcopilot.evals.application.insights import capture_source_revision, result_insights, source_revision


def fixture_result() -> tuple[dict, dict]:
    request = {"evaluation_id": "eval-fixture", "kind": "suite", "bot_id": "fixture-bot",
               "suite_id": "fixture-suite", "repetitions": 1, "seed": 0, "max_wall_seconds": 600,
               "bot_spec_sha256": "config-a", "case_ids": ["a", "b"], "dry_run": False}
    result = {"kind": "suite", "selected_cases": ["fixture-suite:a", "fixture-suite:b"],
              "targets": [{"target_id": "main", "executor": "agent_configured", "backend": "native",
                           "model": "fixture-model", "reasoning_effort": "", "config_fingerprint": "runtime-a"}],
              "summary": {"verdict": "failed"},
              "config_snapshot": {"case_hash": "cases-a", "judge": "suite-or-profile-defined",
                                  "environment_fingerprint": "env-a", "definition_snapshot": {
                                      "manifest": {"suite_id": "fixture-suite", "version": "1"},
                                      "protocols": {"driver": "1", "scorer": "1"},
                                      "execution_implementations": {"scorer": "source-a"},
                                      "target_fingerprint": {"main": "target-a"},
                                      "environment_identity": {"private_hash": "env-a"},
                                      "base_fingerprint": "full-a"}},
              "trials": [{"trial_id": f"trial-{case}", "evaluation_id": "eval-fixture", "case_ref": f"fixture-suite:{case}",
                          "case_id": case, "target_id": "main", "attempt": 1,
                          "outcome": "passed" if case == "a" else "failed", "dimension": "fixture-capability"}
                         for case in ("a", "b")]}
    return request, result


def insight(request, result, status="completed", planned=2):
    return result_insights(request, result, status=status, planned=planned)


def test_result_statistics_preserve_failure_and_count_all_outcomes():
    request, result = fixture_result()
    value = insight(request, result)
    assert value["counts"] == {"passed": 1, "failed": 1, "error": 0, "skipped": 0}
    assert value["pass_rate"] == 0.5
    assert value["verdict"] == "failed"
    assert value["trend_eligible"] is True
    for outcome in ("error", "skipped"):
        result["trials"][1]["outcome"] = outcome
        value = insight(request, result)
        assert value["pass_rate"] == 0.5
        assert value["counts"][outcome] == 1
        assert value["trend_eligible"] is True


@pytest.mark.parametrize("status", ["queued", "running", "partial", "cancelled", "interrupted", "error"])
def test_only_terminal_complete_evaluations_enter_trend(status):
    request, result = fixture_result()
    value = insight(request, result, status)
    assert value["counts"]["passed"] == 1
    assert value["trend_eligible"] is False
    assert value["exclusion_reason"] == "not_completed"


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "cross_evaluation", "missing_case", "wrong_case", "dry_run"])
def test_incomplete_or_invalid_records_cannot_form_success_trends(mutation):
    request, result = fixture_result()
    if mutation == "duplicate":
        result["trials"][1] = copy.deepcopy(result["trials"][0])
    elif mutation == "unknown":
        result["trials"][1]["outcome"] = "unknown"
    elif mutation == "cross_evaluation":
        result["trials"][1]["evaluation_id"] = "eval-other"
    elif mutation == "missing_case":
        result["trials"].pop()
    elif mutation == "wrong_case":
        result["trials"][1]["case_ref"] = "fixture-suite:other"
    else:
        request["dry_run"] = True
    assert insight(request, result)["trend_eligible"] is False


def test_repetitions_are_counted_and_missing_attempts_are_detected():
    request, result = fixture_result()
    request["repetitions"] = 2
    for trial in list(result["trials"]):
        result["trials"].append({**trial, "trial_id": trial["trial_id"] + "-2", "attempt": 2})
    value = insight(request, result, planned=4)
    assert value["counts"]["passed"] == 2
    assert value["pass_rate"] == 0.5
    assert value["trend_eligible"] is True
    result["trials"][-1]["attempt"] = 3
    assert insight(request, result, planned=4)["trend_eligible"] is False


@pytest.mark.parametrize("field", ["case_hash", "judge", "implementation", "model", "backend", "repetitions", "seed", "budget", "options", "cases"])
def test_changed_test_conditions_split_series(field):
    request, result = fixture_result()
    original = insight(request, result)["series_key"]
    if field in ("case_hash", "judge"):
        result["config_snapshot"][field] += "-changed"
    elif field == "implementation":
        result["config_snapshot"]["definition_snapshot"]["execution_implementations"]["scorer"] = "source-b"
    elif field in ("model", "backend"):
        result["targets"][0][field] += "-changed"
    elif field in ("repetitions", "seed"):
        request[field] += 1
    elif field == "budget":
        request["max_wall_seconds"] += 1
    elif field == "options":
        request["options"] = {"fixture_option": True}
    else:
        result["selected_cases"].append("fixture-suite:other")
    assert insight(request, result)["series_key"] != original


def test_code_and_runtime_configuration_are_annotated_without_splitting_benchmark():
    request, result = fixture_result()
    original = insight(request, result)
    request["bot_spec_sha256"] = "config-b"
    request["source_revision"] = {"commit": "a" * 40}
    result["targets"][0]["config_fingerprint"] = "runtime-b"
    definition = result["config_snapshot"]["definition_snapshot"]
    definition["target_fingerprint"]["main"] = "target-b"
    definition["environment_identity"] = {"private_hash": "env-b"}
    definition["base_fingerprint"] = "full-b"
    current = insight(request, result)
    assert current["series_key"] == original["series_key"]
    assert current["configuration_fingerprint"] != original["configuration_fingerprint"]


def test_missing_definition_and_missing_results_remain_explicit():
    request, result = fixture_result()
    result["config_snapshot"] = {}
    value = insight(request, result)
    assert value["exclusion_reason"] == "missing_definition"
    assert value["pass_rate"] == 0.5
    value = insight(request, {})
    assert value["counts"] is None
    assert value["pass_rate"] is None
    assert value["trend_eligible"] is False
    result = {"summary": {"outcomes": {"passed": 2, "failed": 0, "error": 0, "skipped": 0}}}
    value = insight(request, result)
    assert value["counts"]["passed"] == 2
    assert value["source"] == "summary"
    assert value["trend_eligible"] is False


@pytest.mark.parametrize("dirty", [True, False])
def test_source_capture_stores_commit_and_dirty_without_paths(tmp_path, dirty):
    def run(command, **kwargs):
        assert "remote" not in command
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n")
        if dirty:
            kwargs["stdout"].write(b" M private-fixture-filename\n")
        return subprocess.CompletedProcess(command, 0)
    with patch("chatcopilot.evals.application.insights.subprocess.run", side_effect=run):
        value = capture_source_revision(tmp_path)
    assert value["commit"] == "a" * 40
    assert value["dirty"] is dirty
    assert value["status"] == "recorded"
    assert value["captured_at"]
    assert "private-fixture-filename" not in json.dumps(value)


def test_no_git_and_historical_missing_revision_do_not_fabricate_version(tmp_path):
    with patch("chatcopilot.evals.application.insights.subprocess.run", side_effect=FileNotFoundError):
        value = capture_source_revision(tmp_path)
    assert value["commit"] is None
    assert value["status"] == "unavailable"
    assert source_revision({}) == {"status": "not_recorded", "commit": None, "dirty": None, "captured_at": None}


def test_persisted_insights_are_read_only_after_application_restart(tmp_path):
    request, result = fixture_result()
    request["created_at"] = "2026-09-01T00:00:00Z"
    request["source_revision"] = {"commit": "a" * 40, "dirty": True, "status": "recorded", "captured_at": request["created_at"]}
    result["evaluation_id"] = request["evaluation_id"]
    directory = tmp_path / request["evaluation_id"]
    directory.mkdir(mode=0o700)
    for name, value in [("request", request), ("result", result), ("state", {"evaluation_id": request["evaluation_id"], "status": "completed"})]:
        path = directory / f"{name}.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with patch("chatcopilot.evals.application.controller.capture_source_revision", side_effect=AssertionError("read must not capture current Git")):
        manager = EvaluationApplication(tmp_path)
        record = manager.list()[0]
    assert record["source_revision"]["commit"] == "a" * 40
    assert record["insights"]["trend_eligible"] is True
    assert "result" not in record
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


@pytest.mark.parametrize("changes", [
    {"commit": "invalid"}, {"dirty": "false"}, {"captured_at": "yesterday"},
    {"captured_at": "2026-09-01T00:00:00"}, {"status": "unknown"}, {"extra": "not allowed"},
])
def test_source_revision_metadata_is_strictly_validated(changes):
    from chatcopilot.evals.source_revision import validate_source_revision
    value = {"status": "recorded", "commit": "a" * 40, "dirty": True, "captured_at": "2026-09-01T00:00:00Z"}
    assert validate_source_revision(value) == value
    with pytest.raises(ValueError, match="source revision"):
        validate_source_revision({**value, **changes})


def test_target_metrics_and_quality_coverage_do_not_mix_targets():
    request, result = fixture_result()
    result["trials"][0]["evidence"] = {"agent_duration_seconds": 3, "judge_evidence": {
        "quality_applicable": True, "metrics": [{"kind": "quality", "score": 0.9, "error": None}]}}
    result["trials"][1]["evidence"] = {"agent_duration_seconds": 5, "judge_evidence": {
        "quality_applicable": True, "metrics": [{"kind": "quality", "score": None, "error": "timeout"}]}}
    value = insight(request, result)
    assert value["quality"] == {"score": 0.9, "scored": 1, "expected": 2}
    lane = value["targets"][0]
    assert lane["pass_rate"] == 0.5 and lane["agent_duration_seconds"] == 8
    assert len(lane["cases"]) == 2
    assert lane["cases"][1]["quality"]["score"] is None


def test_trial_preview_contains_actual_input_and_output_without_tool_bodies():
    from chatcopilot.evals.application.insights import trial_preview
    value = trial_preview({"trial_id": "one", "final_text": "answer" * 1000,
        "evidence": {"execution": {"state": "recorded", "turns": [{"input": "actual input"}]},
                     "tool_calls": [{"result": "large private body"}]}})
    assert value["input_preview"] == "actual input" and len(value["final_text"]) == 400
    assert value["body_available"] is True
    assert "large private body" not in json.dumps(value)
