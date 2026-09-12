"""Generic file-authored tasks using fixed tools and strict DeepEval judgments."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from chatcopilot.evals.business_dataset import agent_inputs, load_business_cases, tool_dependencies
from chatcopilot.evals.business_scoring import score_business
from chatcopilot.evals.business_tools import build_provider, preflight
from chatcopilot.evals.environment_agent import run_environment_agent
from chatcopilot.evals.models import EvalCase, TrialObservation
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.trial_capture import execution_snapshot


@contextmanager
def open_case(
    case: EvalCase, *, bot: str, workspace_root: Path, options: dict[str, Any]
):
    audit: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    final_text = ""
    evidence_state = "not_recorded"
    current_turn = 0
    audit_offset = 0

    def begin_turn(index: int) -> None:
        nonlocal current_turn, audit_offset
        for item in audit[audit_offset:]:
            item["turn_index"] = current_turn
        audit_offset = len(audit)
        current_turn = index

    provider = build_provider(case, audit)
    inputs = agent_inputs(case)
    final_text, events = run_environment_agent(
        bot=bot,
        workspace_root=workspace_root,
        task_text=inputs[0],
        turn_texts=inputs,
        turn_callback=begin_turn,
        provider=provider,
        tool_names=frozenset(tool_dependencies(case)),
    )
    begin_turn(current_turn)
    evidence_state = "recorded"
    execution = execution_snapshot()
    stopped = next(
        (
            turn.get("stop_reason")
            for turn in execution.get("turns", [])
            if turn.get("stop_reason") in {"llm_error", "timeout_cap", "cancelled"}
        ),
        None,
    )
    observation = TrialObservation(
        structured_error={"stop_reason": stopped} if stopped else None,
        final_text=final_text,
        events=tuple(events),
        tool_calls=tuple(audit),
        evidence=(
            {
                "kind": "business_capture",
                "execution": execution,
                "tool_evidence_state": evidence_state,
            },
        ),
    )
    from chatcopilot.evals.models import PreparedCase
    yield PreparedCase(observation, lambda: score_business(case, observation))


PLUGIN = EvaluationPlugin(
    plugin_id="business-agent",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=lambda context: load_business_cases(context.manifest),
    preflight=preflight,
    open_case=open_case,
)
