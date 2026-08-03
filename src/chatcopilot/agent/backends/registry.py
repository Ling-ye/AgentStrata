"""Code-owned main-agent backend registry."""
from __future__ import annotations

from collections.abc import Callable

from chatcopilot.contracts.agent_backend import AgentBackend

BackendFactory = Callable[..., AgentBackend]

_BACKEND_IDS = frozenset({"native", "langgraph", "codex"})


def backend_ids() -> frozenset[str]:
    return _BACKEND_IDS


def build_backend(backend_id: str, **kwargs) -> AgentBackend:
    normalized = str(backend_id or "native").strip().lower()
    if normalized in {"native", "langgraph"}:
        from chatcopilot.agent.backends.inprocess import InProcessAgentBackend

        return InProcessAgentBackend(normalized, tool_names=set(kwargs["tool_names"]))
    if normalized == "codex":
        from chatcopilot.agent.backends.codex import CodexAgentBackend

        return CodexAgentBackend(**kwargs)
    raise ValueError(
        f"unsupported agent backend: {backend_id!r}; expected one of "
        + ", ".join(sorted(_BACKEND_IDS))
    )


__all__ = ["backend_ids", "build_backend"]

