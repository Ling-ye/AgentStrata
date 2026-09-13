from __future__ import annotations

import copy

from chatcopilot.harness.evidence_context import evidence_index


def test_late_failure_and_missing_evidence_stay_discoverable():
    evidence = {
        "source": {
            "expected_behavior": "original expectation",
            "feedback": {"repair_hint": "untrusted"},
        },
        "verification": {
            "rows": [{"outcome": "passed"}] * 4000
            + [{"outcome": "failed", "error_code": "wrong_result"}, {"outcome": "not_run"}],
            "missing": ["external_confirmation"],
        },
    }
    original = copy.deepcopy(evidence)
    index = evidence_index(evidence, stage="review")
    assert evidence == original
    assert index["observed_status_counts"] == {"passed": 4000, "failed": 1, "not_run": 1}
    assert "/verification/rows/4000/outcome" in {
        row["pointer"] for row in index["adverse_locations"]
    }
    assert index["inline_sections"]["source"] == evidence["source"]
    assert "verification" not in index["inline_sections"]
    assert index["missing_stage_sections"] == ["patch", "regression", "reproduction"]


def test_partial_adverse_index_is_explicit_and_paths_are_json_pointers():
    index = evidence_index({"a/b~c": [{"outcome": "failed"}] * 40}, stage="repair")
    assert index["adverse_field_count"] == 40
    assert index["adverse_locations_partial"] is True
    assert index["adverse_locations"][0]["pointer"] == "/a~1b~0c/0/outcome"
    assert "source" in index["missing_stage_sections"]


def test_empty_evidence_does_not_imply_success():
    index = evidence_index({}, stage="prepare")
    assert index["observed_status_counts"] == {}
    assert index["missing_stage_sections"] == ["source", "reproduction"]
