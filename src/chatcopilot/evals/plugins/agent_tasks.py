"""One declarative task suite, executed and scored inside Core-owned Trials."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from chatcopilot.evals.agent_tasks.scenes import validate
from chatcopilot.evals.deepeval_engine import score
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.plugins.generic_agent import load_declarative_cases
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.trial_capture import execution_snapshot


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


@contextmanager
def open_case(case, *, bot: str, workspace_root: Path, options):
    from chatcopilot.evals.agent_tasks.runtime import run
    from chatcopilot.evals.models import PreparedCase

    definition = next(d for d in load_case_definitions(get_manifest("agentstrata-agent-tasks-v1"))
                      if d.case_id == case.case_id)
    if options.get("scoring_mode", "native_geval") != "native_geval":
        raise ValueError("required semantic criteria cannot be disabled")
    observation = run(definition, suite_id="agentstrata-agent-tasks-v1", bot=bot, workspace_root=workspace_root)
    captured = execution_snapshot()
    if (captured.get("state") != "recorded" or len(captured.get("turns", [])) != len(definition.turns)
            or any(t.get("state") != "recorded" or not t.get("completed") for t in captured.get("turns", []))):
        raise ValueError("task execution evidence is missing or truncated")
    yield PreparedCase(observation, lambda: score(definition, observation))


PLUGIN = EvaluationPlugin(
    plugin_id="agent-tasks",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=load_declarative_cases,
    preflight=preflight,
    open_case=open_case,
)
