"""Source-bound comparisons keep grading fixed and never turn missing facts into gains."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json

import pytest

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.evals import version_snapshot
from chatcopilot.evals.models import EvalCase, EvaluationTarget, JudgeResult, PreparedCase, TrialObservation
from chatcopilot.evals.report import compare_reports
from chatcopilot.evals.trial_runner import run_case
from tests.evaluation_fixtures import result_payload


def _version(target, source):
    value = {"schema": version_snapshot.SCHEMA, "source_sha256": source * 64,
        "execution_sha256": source * 64, "verification_sha256": "c" * 64,
        "targets": {target["target_id"]: target["fingerprint"]},
        "runtime": {"python": "3.13", "packages": {"langgraph": "1.2.2"}}, "product_components": {}}
    return {**value, "snapshot_ref": digest(value)}


def _report(identifier, outcomes, *, bound=False, candidate=False):
    fingerprint = digest("candidate" if candidate else "baseline")
    target = {"target_id": "main", "executor": "agent_configured", "runtime_id": "langgraph",
              "model": "fixed-model", "reasoning_effort": "", "config_fingerprint": fingerprint,
              "fingerprint": fingerprint}
    snapshot = {"case_hash": "fixed-cases", "judge": "fixed-judge", "target_fingerprints": {"main": fingerprint},
        "definition_fingerprint": fingerprint, "configuration_invariants": "fixed-authority-and-model",
        "environment_fingerprint": "environment", "private_runtime_configuration": {"identity_hmac": "same"},
        "definition_snapshot": {"cases": [{"case_id": name, "definition_sha256": name} for name in outcomes],
            "manifest": {"suite_id": "fixture-suite"}, "protocols": {"scorer": "frozen"},
            "plugin": "frozen-plugin", "execution_implementations": {"sha256": "same-verifier"}}}
    if bound:
        snapshot["version_snapshot"] = _version(target, "b" if candidate else "a")
    return result_payload({"evaluation_id": identifier, "kind": "suite", "suite": "fixture-suite",
        "status": "completed", "selected_cases": ["fixture-suite:" + name for name in outcomes],
        "targets": [target], "config_snapshot": snapshot,
        "trials": [{"trial_id": identifier + "-" + name, "case_id": name, "case_ref": "fixture-suite:" + name,
            "target_id": "main", "target_fingerprint": fingerprint, "outcome": outcome,
            "model": "fixed-model",
            "score": 1.0 if outcome == "passed" else 0.0, "max_score": 1.0,
            "passed": outcome == "passed", "evidence": {"error_code": "environment_error"}}
            for name, outcome in outcomes.items()]})


def _compare(tmp_path, baseline, candidate):
    before, after = tmp_path / "base.json", tmp_path / "new.json"
    before.write_text(json.dumps(baseline))
    after.write_text(json.dumps(candidate))
    return compare_reports(before, after)


@pytest.mark.parametrize("before,after", [("error", "passed"), ("passed", "error"), ("skipped", "passed")])
def test_unscored_execution_is_inconclusive_not_improvement_or_regression(tmp_path, before, after):
    result = _compare(tmp_path, _report("base", {"a": before}), _report("new", {"a": after}))
    assert result["comparison_status"] == "inconclusive"
    assert not result["improvements"] and not result["regressions"]
    assert result["score_delta"] is None
    assert result["inconclusive"][0]["base_outcome"] == before


def test_skipped_repetition_cannot_turn_partial_failures_into_an_improvement(tmp_path):
    base = _report("base", {"a": "failed"})
    new = _report("new", {"a": "passed"})
    for result, outcome in ((base, "skipped"), (new, "passed")):
        extra = deepcopy(_report(result["evaluation_id"] + "-repeat", {"a": outcome})["trials"][0])
        extra["evaluation_id"] = result["evaluation_id"]
        extra["attempt"] = 2
        extra["order"] = 2
        result["trials"].append(extra)
    comparison = _compare(tmp_path, base, new)
    assert comparison["comparison_status"] == "inconclusive"
    assert comparison["score_delta"] is None and not comparison["improvements"]


def test_bound_source_changes_can_compare_but_any_regression_blocks_total_gain(tmp_path):
    base = _report("base", {"a": "failed", "b": "failed", "protected": "passed"}, bound=True)
    new = _report("new", {"a": "passed", "b": "passed", "protected": "failed"}, bound=True, candidate=True)
    result = _compare(tmp_path, base, new)
    assert result["comparison_status"] == "regressed"
    assert len(result["improvements"]) == 2 and len(result["regressions"]) == 1
    assert result["version_binding"]["base"] != result["version_binding"]["new"]


def test_same_bound_rules_allow_measured_improvement(tmp_path):
    result = _compare(tmp_path, _report("base", {"a": "failed"}, bound=True),
                      _report("new", {"a": "passed"}, bound=True, candidate=True))
    assert result["comparison_status"] == "improved"


@pytest.mark.parametrize("change", ["unbound", "manifest", "scorer", "environment", "model", "authority", "runtime", "tampered_ref", "target_binding", "reused_trial"])
def test_version_comparison_rejects_unbound_drift_and_forgery(tmp_path, change):
    base = _report("base", {"a": "failed"}, bound=True)
    new = _report("new", {"a": "passed"}, bound=True, candidate=True)
    snapshot = new["config_snapshot"]
    if change == "unbound":
        snapshot.pop("version_snapshot")
    elif change == "manifest":
        snapshot["definition_snapshot"]["manifest"]["suite_id"] = "different"
    elif change == "scorer":
        snapshot["definition_snapshot"]["execution_implementations"]["sha256"] = "different"
    elif change == "environment":
        snapshot["environment_fingerprint"] = "different"
    elif change == "authority":
        snapshot["configuration_invariants"] = "different"
    elif change == "model":
        new["targets"][0]["model"] = "different"
    elif change == "runtime":
        snapshot["version_snapshot"]["runtime"]["packages"]["langgraph"] = "different"
        value = {k: v for k, v in snapshot["version_snapshot"].items() if k != "snapshot_ref"}
        snapshot["version_snapshot"]["snapshot_ref"] = digest(value)
    elif change == "tampered_ref":
        snapshot["version_snapshot"]["snapshot_ref"] = "0" * 64
    elif change == "target_binding":
        new["targets"][0]["fingerprint"] = "reused-for-another-target"
    else:
        new["trials"][0]["target_fingerprint"] = base["targets"][0]["fingerprint"]
    with pytest.raises(ValueError):
        _compare(tmp_path, base, new)


def test_adapter_metadata_cannot_override_core_expectations_or_scoring(tmp_path, monkeypatch):
    @contextmanager
    def adapter(*args, **kwargs):
        yield PreparedCase(TrialObservation(final_text="answer", stop_reason="end_turn", post_state={"actual": True},
            evidence=({"kind": "adapter_metadata", "expectation": "forged", "judge_evidence": {"passed": True},
                       "post_state": {"forged": True}, "case_source": {"forged": True}, "detail": "retained"},)),
            lambda: (JudgeResult(0.0, 1.0, False, reasons=("real failure",)), {"passed": False}))
    monkeypatch.setattr("chatcopilot.evals.case_drivers.open_case", adapter)
    result = run_case(EvalCase("one", "input", "bridge", expected_behavior="frozen goal"), suite_id="suite", bot="bot",
                      workspace_root=tmp_path, options={})
    assert result.status == "failed" and not result.metadata["judge_evidence"]["passed"]
    assert result.metadata["expectation"].behavior == "frozen goal"
    assert result.metadata["post_state"] == {"actual": True}
    assert result.metadata["case_source"] == {} and result.metadata["detail"] == "retained"


def test_source_binding_detects_file_drift_and_survives_redaction(tmp_path, monkeypatch):
    from chatcopilot.evals.redaction import redact_payload
    root = tmp_path / "source"
    paths = {"src/chatcopilot/__init__.py": "", "src/chatcopilot/evals/scorer.py": "RULE = 'trusted'\n",
             "src/chatcopilot/agent/product.py": "VALUE = 1\n", "pyproject.toml": "", "uv.lock": ""}
    manifest = {}
    for name, content in paths.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        manifest[name] = {"sha256": hashlib.sha256(content.encode()).hexdigest(), "executable": False}
    receipt = {"path": str(root), "sha256": manifest_digest(manifest), "execution_sha256": manifest_digest(manifest), "manifest": manifest}
    monkeypatch.setattr(version_snapshot, "__file__", str(root / "src/chatcopilot/evals/version_snapshot.py"))
    target = EvaluationTarget("main", "main", "agent_configured", "langgraph", "model", "", "f" * 64)
    snapshot = version_snapshot.capture(receipt, [target])
    assert redact_payload(snapshot) == snapshot
    version_snapshot.verify(receipt, snapshot, [target])
    (root / "src/chatcopilot/agent/product.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="changed"):
        version_snapshot.verify(receipt, snapshot, [target])
