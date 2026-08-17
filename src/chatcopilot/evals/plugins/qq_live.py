"""Trusted declarative-case binding for Core-owned bounded QQ live execution."""

from chatcopilot.evals.plugins.base import CaseLoadContext, EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.plugins.generic_agent import load_declarative_cases


def _load_cases(context: CaseLoadContext):
    return load_declarative_cases(context, plugin_id="qq-live")


PLUGIN = EvaluationPlugin(
    plugin_id="qq-live",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"qq_live", "dry_run"}),
    load_cases=_load_cases,
)

__all__ = ["PLUGIN"]
