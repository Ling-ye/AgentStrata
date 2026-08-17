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
    from chatcopilot.core.llm_client import LLMClient

    result = LLMClient(chat_config.llm).chat(
        messages=bfcl.build_messages(case),
        tools=bfcl.build_tools_schema(case) or None,
        stream=False,
    )
    return {
        "final_text": result.content or "",
        "tool_calls": _extract_tool_calls(result),
        "usage": result.usage or {},
        "metadata": {
            "bfcl_category": str(case.metadata.get("bfcl_category") or ""),
            "benchmark_category": str(case.metadata.get("bfcl_category") or case.category),
        },
    }


def _judge(case, observation: dict[str, Any]):
    return bfcl.judge(case, observation.get("tool_calls") or [])


def _extract_tool_calls(chat_result: Any) -> list[dict[str, Any]]:
    import json

    calls: list[dict[str, Any]] = []
    for tool_call in getattr(chat_result, "tool_calls", None) or []:
        if isinstance(tool_call, dict):
            calls.append(tool_call)
            continue
        function = getattr(tool_call, "function", None)
        if function is None:
            continue
        arguments = getattr(function, "arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError, json.JSONDecodeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            {
                "name": str(getattr(function, "name", "") or ""),
                "arguments": arguments,
            }
        )
    return calls


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
