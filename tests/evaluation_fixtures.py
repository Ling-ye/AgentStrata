"""Compact v2 result construction for controlled test scenarios."""

from dataclasses import fields, replace

from chatcopilot.evals.models import (
    Assessment, CaseExpectation, EvaluationError, EvaluationTrial, ExecutionEvidence, JudgeResult,
    to_jsonable,
)


def trial_result(**values):
    """Accept scenario facts, construct the four explicit v2 result sections."""
    names = {field.name for field in fields(EvaluationTrial)}
    identity = {name: value for name, value in values.items() if name in names}
    outcome = values.get("outcome", "error")
    raw_judge = values.get("judge")
    if raw_judge is not None:
        judge = JudgeResult(**raw_judge)
    elif outcome in {"passed", "failed"}:
        judge = JudgeResult(values.get("score", float(outcome == "passed")),
                            values.get("max_score", 1.0), outcome == "passed")
    else:
        judge = None
    evidence = dict(values.get("evidence") or {})
    judging = evidence.pop("judge_evidence", {})
    identity.setdefault("expectation", CaseExpectation(behavior="Controlled expected behavior"))
    identity.setdefault("execution", ExecutionEvidence(
        final_text=values.get("final_text", ""), stop_reason=values.get("stop_reason", ""),
        events=tuple(values.get("events", ())), usage=values.get("usage_totals", {}),
        metadata=evidence, total_seconds=values.get("duration_seconds"),
        started_at=values.get("started_at", ""), finished_at=values.get("finished_at", ""),
    ))
    identity.setdefault("assessment", Assessment(judge, judging) if judge or judging else None)
    raw_error = values.get("error")
    identity["error"] = (raw_error if isinstance(raw_error, EvaluationError) else
        EvaluationError(evidence.get("error_stage", "execution"), evidence.get("error_code", "execution_error"), raw_error or "controlled error") if outcome == "error" else None)
    return EvaluationTrial(**identity)


def replace_trial(trial, **changes):
    identity = {field.name: getattr(trial, field.name) for field in fields(EvaluationTrial)}
    execution = {key: changes.pop(key) for key in list(changes)
                 if key in {"final_text", "stop_reason", "events", "started_at", "finished_at"}}
    if "duration_seconds" in changes:
        execution["total_seconds"] = changes.pop("duration_seconds")
    if "evidence" in changes:
        execution["metadata"] = changes.pop("evidence")
    if execution:
        identity["execution"] = replace(trial.execution, **execution)
    if "score" in changes or "passed" in changes:
        judge = trial.assessment.judge
        identity["assessment"] = replace(trial.assessment, judge=replace(judge,
            **{k: changes.pop(k) for k in ("score", "passed") if k in changes}))
    identity.update(changes)
    return EvaluationTrial(**identity)


def trial_payload(description, *, evaluation_id="eval-fixture", kind="suite"):
    if "execution" in description and "expectation" in description:
        return description
    defaults = dict(trial_id="trial-fixture", evaluation_id=evaluation_id, kind=kind, bot="controlled",
        profile="", suite_id="fixture-suite", case_ref="fixture-suite:a", case_id="a", dimension="test",
        target_id="main", target_fingerprint="fingerprint", executor="agent_configured", backend="native",
        model="controlled", reasoning_effort="", attempt=1, order=1, outcome="passed")
    return to_jsonable(trial_result(**{**defaults, **description}))


def result_payload(description):
    return {**description, "schema_version": 2,
        "trials": [trial_payload(t, evaluation_id=description.get("evaluation_id", "eval-fixture"),
                                 kind=description.get("kind", "suite")) for t in description.get("trials", [])]}


def execute_business(case, **kwargs):
    from chatcopilot.evals.trial_runner import run_case
    return run_case(case, suite_id=case.metadata["suite_id"], **kwargs)


def execute_agent_task(case, **kwargs):
    from chatcopilot.evals.trial_runner import run_case
    return run_case(case, suite_id="agentstrata-agent-tasks-v1", **kwargs)


def run_direct_cases(suite_id, plugin, cases, *, bot, **kwargs):
    from pathlib import Path
    from unittest.mock import patch
    from dataclasses import replace
    from chatcopilot.evals.registry import get_manifest
    from chatcopilot.evals.trial_runner import run_case
    manifest = replace(get_manifest("bfcl"), suite_id=suite_id, plugin_id=plugin.plugin_id)
    with patch("chatcopilot.evals.registry.get_manifest", return_value=manifest), \
         patch("chatcopilot.evals.plugins.get_evaluation_plugin", return_value=plugin):
        return [run_case(case, suite_id=suite_id, bot=bot, workspace_root=Path("/tmp"),
                         options=kwargs.get("options", {}), driver="direct_llm") for case in cases]
