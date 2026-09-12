"""One declarative task suite, executed and scored inside Core-owned Trials."""

from __future__ import annotations

from pathlib import Path
import time
from chatcopilot.evals.agent_tasks.scenes import validate
from chatcopilot.evals.deepeval_engine import score
from chatcopilot.evals.execution_support import usage_summary
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.plugins.generic_agent import load_declarative_cases
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.models import EvalCaseResult
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.trial_capture import capture_case, set_phase, execution_snapshot


def preflight(*, cases):
    definitions = {
        d.case_id: d for d in load_case_definitions(get_manifest("agentstrata-agent-tasks-v1"))
    }
    for case in cases:
        raw = case.metadata["case_definition"]
        # Use the packaged definition, never model-supplied scenario configuration.
        definition = definitions[case.case_id]
        validate(definition)
        if raw["scenario_id"] != definition.scenario_id:
            raise ValueError("scenario definition mismatch")


@capture_case
def execute(case, *, bot: str, workspace_root: Path, options):
    from chatcopilot.evals.agent_tasks.runtime import run

    started = time.monotonic()
    observation = None
    stage = "execution"
    suite_id = "agentstrata-agent-tasks-v1"
    try:
        definition = next(
            d for d in load_case_definitions(get_manifest(suite_id)) if d.case_id == case.case_id
        )
        if options.get("scoring_mode", "native_geval") != "native_geval":
            raise ValueError("required semantic criteria cannot be disabled")
        observation = run(definition, suite_id=suite_id, bot=bot, workspace_root=workspace_root)
        stage = "evidence"
        captured = execution_snapshot()
        if (
            captured.get("state") != "recorded"
            or len(captured.get("turns", [])) != len(definition.turns)
            or any(
                t.get("state") != "recorded" or not t.get("completed")
                for t in captured.get("turns", [])
            )
        ):
            raise ValueError("task execution evidence is missing or truncated")
        stage = "judging"
        set_phase(stage)
        judge, evidence = score(definition, observation)
        error = evidence.get("error") or ""
        return EvalCaseResult(
            case_id=case.case_id,
            suite_id=suite_id,
            status="error" if error else "passed" if judge.passed else "failed",
            score=judge.score,
            max_score=judge.max_score,
            judge=judge,
            final_text=observation.final_text,
            stop_reason=observation.stop_reason,
            events=observation.events,
            duration_seconds=time.monotonic() - started,
            error=error or None,
            metadata={
                **usage_summary(list(observation.events)),
                "judge_evidence": evidence,
                "tool_calls": list(observation.tool_calls),
                "task_state": observation.post_state,
                "error_stage": stage if error else "",
            },
        )
    except Exception as exc:
        return EvalCaseResult(
            case_id=case.case_id,
            suite_id=suite_id,
            status="error",
            error=str(exc),
            final_text=observation.final_text if observation else "",
            events=observation.events if observation else (),
            duration_seconds=time.monotonic() - started,
            metadata={
                "error_stage": stage,
                "judge_evidence": {
                    "quality_applicable": bool(
                        case.metadata["case_definition"]["quality"]["enabled"]
                    ),
                    "metrics": [],
                },
            },
        )


PLUGIN = EvaluationPlugin(
    plugin_id="agent-tasks",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=load_declarative_cases,
    preflight=preflight,
    execute_trial=execute,
)
