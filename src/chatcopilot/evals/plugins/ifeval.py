"""Thin trusted binding for the existing IFEval adapter."""

from chatcopilot.evals.adapters import ifeval
from chatcopilot.evals.plugins.base import CaseLoadContext, EvaluationPlugin, PLUGIN_API_VERSION


def _load_cases(context: CaseLoadContext):
    del context
    return ifeval.load_cases()


def _judge(case, final_text: str, *, chat_config=None):
    del chat_config
    return ifeval.judge(case, final_text)


PLUGIN = EvaluationPlugin(
    plugin_id="ifeval",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=_load_cases,
    judge=_judge,
)

__all__ = ["PLUGIN"]
