"""Deterministic judges for evaluation cases."""

from __future__ import annotations

from chatcopilot.evals.models import EvalCase, JudgeResult


def judge_rules(case: EvalCase, final_text: str) -> JudgeResult:
    """Score a case with simple must-have / must-not checks."""

    text = final_text.lower()
    missing = tuple(item for item in case.must_have if item.lower() not in text)
    violations = tuple(item for item in case.must_not if item.lower() in text)
    total_checks = len(case.must_have) + len(case.must_not)
    if total_checks == 0:
        return JudgeResult(score=1.0, max_score=1.0, passed=True, reasons=("no rule checks",))

    passed_checks = total_checks - len(missing) - len(violations)
    score = max(0.0, passed_checks / total_checks)
    reasons: list[str] = []
    if missing:
        reasons.append("missing required evidence")
    if violations:
        reasons.append("matched forbidden content")
    if not reasons:
        reasons.append("all rule checks passed")
    return JudgeResult(
        score=score,
        max_score=1.0,
        passed=not missing and not violations,
        reasons=tuple(reasons),
        missing=missing,
        violations=violations,
    )
