"""Thin trusted binding for the existing GAIA adapter."""

from chatcopilot.evals.adapters import gaia
from chatcopilot.evals.plugins.base import CaseLoadContext, EvaluationPlugin, PLUGIN_API_VERSION


def _load_cases(context: CaseLoadContext):
    return gaia.load_cases(auto_download=context.auto_prepare)


def _judge(case, final_text: str, *, chat_config=None):
    result = gaia.judge(case, final_text)
    if result.passed or chat_config is None:
        return result
    from chatcopilot.evals.judges_llm import judge_llm_rubric

    return judge_llm_rubric(case, final_text, chat_config)


PLUGIN = EvaluationPlugin(
    plugin_id="gaia",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=_load_cases,
    prepare=gaia.prepare_data,
    build_task=gaia.prepare_task,
    judge=_judge,
)

__all__ = ["PLUGIN"]
