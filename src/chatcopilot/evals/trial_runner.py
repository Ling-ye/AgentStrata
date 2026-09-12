"""Single-Case pipeline: adapters provide observations and scoring, Core builds results."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any

from chatcopilot.evals.models import PreparedCase
from chatcopilot.evals.expectations import case_expectation
from chatcopilot.evals.models import (
    Assessment, EvalCase, EvalCaseResult, EvaluationError, JudgeResult, TrialObservation,
)
from chatcopilot.evals.result_codec import (
    PipelineFailure, ResultContractError, assessment_from_dict, error_from_exception,
)
from chatcopilot.evals.trial_capture import (
    capture_case, execution_snapshot, record_checkpoint, set_phase,
)



@capture_case
def run_case(case: EvalCase, *, suite_id: str, bot: str, workspace_root: Path,
             options: dict[str, Any], driver: str = "", dry_run: bool = False,
             confirm_external_write: bool = False, profile_request: Any = None) -> EvalCaseResult:
    from chatcopilot.evals.case_drivers import open_case

    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    observation = TrialObservation()
    assessment = None
    error = None
    phase = "execution"
    expectation = case_expectation(case)
    record_checkpoint("expectation", expectation)
    if dry_run:
        return EvalCaseResult(case.case_id, suite_id, "skipped", started_at=started_at,
                              finished_at=started_at, metadata={"dry_run": True})
    try:
        with open_case(case, suite_id=suite_id, bot=bot, workspace_root=workspace_root,
                       options=options, driver=driver, confirm_external_write=confirm_external_write,
                       profile_request=profile_request) as prepared:
            if not isinstance(prepared, PreparedCase) or not isinstance(prepared.observation, TrialObservation):
                raise ResultContractError("adapter.observation: expected PreparedCase/TrialObservation")
            observation = prepared.observation
            record_checkpoint("observation", observation)
            if observation.stop_reason in {"llm_error", "timeout_cap", "cancelled"}:
                raise PipelineFailure(EvaluationError("execution", "execution_error", observation.stop_reason))
            phase = "scoring"
            set_phase(phase)
            scoring_started = time.monotonic()
            scored_judge, evidence = prepared.score()
            if not isinstance(scored_judge, JudgeResult) or not isinstance(evidence, dict):
                raise ResultContractError("adapter.assessment: expected JudgeResult and evidence")
            assessment = Assessment(scored_judge, evidence, time.monotonic() - scoring_started)
            record_checkpoint("assessment", assessment)
            if evidence.get("error"):
                error = EvaluationError(phase, "judge_error", str(evidence["error"])[:4096])
                assessment = Assessment(None, evidence, assessment.duration_seconds)
                record_checkpoint("assessment", assessment)
            phase = "cleanup"
            set_phase(phase)
        phase = "result_validation"
        set_phase(phase)
    except Exception as exc:
        error = error_from_exception(exc, phase)
        if assessment is None:
            assessment = assessment_from_dict(execution_snapshot().get("assessment"))
        # Some scorers provide partial native/quality evidence with their exception.
        partial = getattr(exc, "evidence", None)
        if isinstance(partial, dict):
            assessment = Assessment(assessment.judge if assessment else None, partial,
                                    assessment.duration_seconds if assessment else None)
    snapshot = execution_snapshot()
    if not observation.final_text and snapshot.get("turns"):
        last = snapshot["turns"][-1]
        if last.get("completed"):
            from dataclasses import replace
            observation = replace(observation, final_text=last.get("final_text", ""),
                                  stop_reason=last.get("stop_reason", ""))
    judging = dict(assessment.evidence) if assessment else {}
    policy = case.metadata.get("case_definition", {}).get("quality", {})
    if type(policy.get("enabled")) is bool:
        judging.setdefault("quality_applicable", policy["enabled"])
        judging.setdefault("quality_reason", policy.get("reason", ""))
    elif "business" in case.metadata:
        judging.setdefault("quality_applicable", True)
        judging.setdefault("primary", "llm_judge")
    judging.setdefault("metrics", [])
    if assessment:
        judging["duration_seconds"] = assessment.duration_seconds
    judge = assessment.judge if assessment else None
    return EvalCaseResult(
        case_id=case.case_id, suite_id=suite_id,
        status="error" if error else "passed" if judge and judge.passed else "failed",
        judge=judge, score=judge.score if judge else 0.0, max_score=judge.max_score if judge else 1.0,
        final_text=observation.final_text, stop_reason=observation.stop_reason,
        events=observation.events, error=error, duration_seconds=time.monotonic() - started,
        started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
        metadata={"expectation": expectation, "judge_evidence": judging, "tool_calls": list(observation.tool_calls),
                  "task_state": observation.post_state, "post_state": observation.post_state,
                  "observation_evidence": list(observation.evidence),
                  "produced_resources": list(observation.produced_resources),
                  "usage_totals": observation.usage, "model_timing": observation.model_timing,
                  "case_source": case.metadata.get("case_source", {}),
                  **{key: value for entry in observation.evidence if entry.get("kind") == "adapter_metadata"
                     for key, value in entry.items() if key != "kind"}},
    )
