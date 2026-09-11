"""Comparable evaluation conditions, independent of a candidate's source identity."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def evaluation_conditions(
    snapshot: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    definition = snapshot.get("definition_snapshot", {})
    if not isinstance(definition, Mapping) or not isinstance(definition.get("cases"), list):
        raise ValueError("frozen per-Case definitions are unavailable; run a new Suite evaluation")
    cases = {case["case_id"]: case["definition_sha256"] for case in definition["cases"]}
    if not cases:
        raise ValueError("frozen Case set is empty")
    return {
        "cases": cases,
        "grading": {
            key: definition.get(key)
            for key in ("manifest", "protocols", "scoring", "quality_rubrics", "scoring_version")
        },
        "targets": {
            str(target["target_id"]): {
                key: target.get(key)
                for key in ("model", "backend", "reasoning_effort", "config_fingerprint")
            }
            for target in targets
        },
        "environment": snapshot.get("environment_fingerprint"),
    }


def verify_conditions(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if expected.get("grading") != actual.get("grading") or expected.get(
        "environment"
    ) != actual.get("environment"):
        raise ValueError("evaluation grading or environment differs from the source evaluation")
    for category in ("cases", "targets"):
        if not isinstance(expected.get(category), dict) or not actual.get(category):
            raise ValueError("evaluation condition identity is incomplete")
        for key, value in actual[category].items():
            if expected[category].get(key) != value:
                raise ValueError(f"evaluation {category} changed: {key}")
