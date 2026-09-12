"""Trusted assertions over the actual Agent trace and isolated final state."""
from __future__ import annotations

import json
from typing import Any

from chatcopilot.evals.agent_case import SUITE, validate_case
from chatcopilot.evals.models import EvalCase, JudgeResult, TrialObservation


def score(case: EvalCase, observation: TrialObservation) -> tuple[JudgeResult, dict[str, Any]]:
    declaration = validate_case(case.metadata["agent_case"])
    checks = []
    for check in declaration["assertions"]:
        kind = check["kind"]
        calls = [c for c in observation.tool_calls if c["name"] == check.get("name")]
        if kind == "final_contains":
            passed = check["value"] in observation.final_text
        elif kind == "final_not_contains":
            passed = check["value"] not in observation.final_text
        elif kind == "tool_called":
            passed = any(all(c["arguments"].get(k) == v for k, v in check.get("arguments", {}).items()) for c in calls)
        elif kind == "tool_not_called":
            passed = not calls
        elif kind == "tool_result_contains":
            passed = any(c["ok"] and check["value"] in json.dumps(c["result"], ensure_ascii=False) for c in calls)
        else:
            state = observation.post_state.get(check["path"])
            if state is None:
                raise ValueError("required post-state evidence is missing")
            passed = state["exists"] and (kind == "file_exists" or state["text"] == check["value"])
        checks.append({"assertion": check, "passed": bool(passed)})
    passed = all(item["passed"] for item in checks)
    facts = JudgeResult(float(passed), 1.0, passed,
                        reasons=tuple("failed: " + item["assertion"]["kind"] for item in checks if not item["passed"]))
    evidence = {"assertions": checks, "quality_applicable": declaration["semantic"], "metrics": []}
    if declaration["semantic"]:
        from chatcopilot.evals.benchmark_scoring import score_benchmark
        _, semantic = score_benchmark(SUITE, case, observation.final_text, lambda: facts,
                                      options={"scoring_mode": "native_geval"}, tool_calls=list(observation.tool_calls))
        evidence.update(semantic)
        metrics = [m for m in semantic["metrics"] if m["kind"] == "quality"]
        if not metrics or any(m.get("error") or m.get("passed") is None for m in metrics):
            evidence["error"] = "semantic judge did not produce valid evidence"
            return facts, evidence
        passed = facts.passed and all(m["passed"] is True for m in metrics)
        return JudgeResult(float(passed), 1.0, passed,
                           reasons=(*facts.reasons, *(str(m["reason"]) for m in metrics))), evidence
    return facts, evidence
