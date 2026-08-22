"""Shared stateless event, usage, and environment helpers for Evaluation executors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from chatcopilot.contracts.agent import AgentEvent, LlmCallFinished
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.evals.env import normalize_eval_env_value
from chatcopilot.evals.event_projection import project_evaluation_event


def event_to_dict(event: AgentEvent) -> dict[str, Any]:
    return project_evaluation_event(event)


def usage_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for event in events:
        if event.get("type") != LlmCallFinished.__name__:
            continue
        usage = event.get("usage") or {}
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if not isinstance(nested_value, int):
                        continue
                    totals[nested_key] = totals.get(nested_key, 0) + nested_value
                    flat_key = f"{key}.{nested_key}"
                    totals[flat_key] = totals.get(flat_key, 0) + nested_value
    return {"usage_totals": totals} if totals else {}


def load_local_env(path: Path) -> None:
    """Load a Bot's local evaluation env without replacing inherited values."""

    if os.environ.get("CHATCOPILOT_EVALUATION_ENV_SNAPSHOT") == "1":
        return
    values = load_local_env_values(path, missing_ok=True, expand_home=True)
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = normalize_eval_env_value(key, value)


__all__ = ["event_to_dict", "load_local_env", "usage_summary"]
