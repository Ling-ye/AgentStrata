"""Agent-level subagent delegation.

Exports are resolved lazily so importing the lightweight ``spec`` module (used by
``botspec`` for declarative tool selectors) does not eagerly pull in the runner /
session machinery, which would create an import cycle through ``agent.context``.
"""

from typing import Any

__all__ = ["SubagentRuntimeConfig", "SubagentRunner", "build_subagent_tools"]


def __getattr__(name: str) -> Any:
    if name == "build_subagent_tools":
        from chatcopilot.agent.subagents.registry import build_subagent_tools

        return build_subagent_tools
    if name in {"SubagentRunner", "SubagentRuntimeConfig"}:
        from chatcopilot.agent.subagents import runner as _runner

        return getattr(_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
