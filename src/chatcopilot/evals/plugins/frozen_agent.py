"""Static plugin for service-registered declarations and adopted Agent regressions."""
from contextlib import contextmanager
from pathlib import Path

from chatcopilot.evals.agent_case import evaluation_cases, load_regressions, validate_case
from chatcopilot.evals.models import PreparedCase
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION


def load_cases(context):
    repository = Path(__file__).resolve().parents[4]
    return tuple(case for snapshot in load_regressions(repository) for case in evaluation_cases(snapshot))


def preflight(*, cases):
    from chatcopilot.evals.deepeval_engine import JudgeConfig, preflight as judge_preflight
    if any(validate_case(case.metadata["agent_case"])["semantic"] for case in cases):
        judge_preflight([])
        JudgeConfig.from_environment()


@contextmanager
def open_case(case, *, bot, workspace_root, options):
    from chatcopilot.evals.frozen_agent_runtime import run
    from chatcopilot.evals.frozen_agent_scoring import score
    observation = run(case, bot=bot, workspace_root=workspace_root)
    yield PreparedCase(observation, lambda: score(case, observation))


PLUGIN = EvaluationPlugin(plugin_id="frozen-agent", api_version=PLUGIN_API_VERSION,
    implementation_module=__name__, allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=load_cases, preflight=preflight, open_case=open_case)
