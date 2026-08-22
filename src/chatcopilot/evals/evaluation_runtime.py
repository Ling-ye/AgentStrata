"""Bot runtime loading and tool projection for isolated Evaluation execution."""

from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from chatcopilot.botspec import assemble_runtime_context, load_botspec, resolve_bot_spec_path
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.evals.execution_support import load_local_env


def load_evaluation_runtime(
    bot: str,
    *,
    load_local_environment: bool = True,
    inherit_environment: bool = True,
) -> Any:
    """Load one Bot runtime with explicit control over machine-local inputs."""

    if load_local_environment and not inherit_environment:
        raise ValueError("isolated Evaluation runtime cannot load bot-local environment")
    candidate: str | Path = Path(bot) if any(char in bot for char in ("/", "\\")) else bot
    environment = nullcontext() if inherit_environment else patch.dict(os.environ, {}, clear=True)
    with environment:
        runtime = assemble_runtime_context(load_botspec(resolve_bot_spec_path(candidate)))
        if load_local_environment:
            load_local_env(runtime.source_path.parent / "local.env")
    return runtime


def permission_filter(allowed: frozenset[str]) -> Callable[[ToolDef], str | None]:
    """Deny every tool not explicitly listed by the frozen Evaluation Case."""

    def check(tool: ToolDef) -> str | None:
        if tool.name in allowed:
            return None
        return "evaluation policy denies this tool"

    return check


__all__ = ["load_evaluation_runtime", "permission_filter"]
