"""Thin trusted binding for the existing BFCL adapter.

BFCL deliberately exercises the configured chat model directly.  Keeping the
request construction and scoring hook on the plugin makes that execution scope
explicit and prevents a future ``direct_llm`` suite from being silently routed
through BFCL merely because it selected the same driver.
"""

from __future__ import annotations

from typing import Any

from chatcopilot.evals.adapters import bfcl
from chatcopilot.evals.plugins.base import CaseLoadContext, EvaluationPlugin, PLUGIN_API_VERSION


def _load_cases(context: CaseLoadContext):
    options = context.options or {}
    category = options.get("category")
    return bfcl.load_cases(category=str(category) if category is not None else None)


def _execute_trial(case, *, chat_config) -> dict[str, Any]:
    from chatcopilot.evals.model_io import invoke

    observation = invoke(case, chat_config=chat_config, messages=bfcl.build_messages(case),
                         tools=bfcl.build_tools_schema(case) or None)
    observation["metadata"].update(bfcl_category=case.metadata["bfcl_category"],
                                    benchmark_category=case.metadata["bfcl_category"],
                                    function_name_mapping=case.metadata.get("function_name_mapping", {}))
    return observation


def _judge(case, observation: dict[str, Any]):
    return bfcl.judge(case, observation.get("tool_calls") or [])



PLUGIN = EvaluationPlugin(
    plugin_id="bfcl",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"direct_llm", "dry_run"}),
    load_cases=_load_cases,
    execute_trial=_execute_trial,
    judge=_judge,
)

__all__ = ["PLUGIN"]
