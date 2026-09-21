"""Code-owned main-agent runtime registry."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from chatcopilot.contracts.runtime_adapter import RUNTIME_IDS, RuntimeAdapter
from chatcopilot.contracts.model_runtime import ResolvedRuntimeRoute


RuntimeFactory = Callable[..., RuntimeAdapter]


class RuntimeAdapterRegistry:
    """The single code-owned dispatch table for main Agent runtimes."""

    def __init__(self, factories: Mapping[str, RuntimeFactory]) -> None:
        if tuple(factories) != RUNTIME_IDS:
            raise ValueError("runtime adapter registry must define the canonical runtime IDs")
        self._factories = MappingProxyType(dict(factories))

    @property
    def runtime_ids(self) -> frozenset[str]:
        return frozenset(self._factories)

    def build(self, route: ResolvedRuntimeRoute, **kwargs: Any) -> RuntimeAdapter:
        factory = self._factories.get(route.runtime_id)
        if factory is None:
            raise ValueError(
                f"unsupported agent runtime: {route.runtime_id!r}; expected one of "
                + ", ".join(RUNTIME_IDS)
            )
        return factory(route=route, **kwargs)


def _native_factory(**kwargs: Any) -> RuntimeAdapter:
    from chatcopilot.agent.runtimes.inprocess import build_native_runtime_adapter

    return build_native_runtime_adapter(**kwargs)


def _langgraph_factory(**kwargs: Any) -> RuntimeAdapter:
    from chatcopilot.agent.runtimes.inprocess import build_langgraph_runtime_adapter

    return build_langgraph_runtime_adapter(**kwargs)


def _codex_factory(**kwargs: Any) -> RuntimeAdapter:
    from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter

    return CodexRuntimeAdapter(**kwargs)


RUNTIME_ADAPTERS = RuntimeAdapterRegistry(
    {
        "native": _native_factory,
        "langgraph": _langgraph_factory,
        "codex": _codex_factory,
    }
)


def runtime_ids() -> frozenset[str]:
    return RUNTIME_ADAPTERS.runtime_ids


def build_runtime_adapter(route: ResolvedRuntimeRoute, **kwargs: Any) -> RuntimeAdapter:
    return RUNTIME_ADAPTERS.build(route, **kwargs)


__all__ = [
    "RUNTIME_ADAPTERS",
    "RuntimeAdapterRegistry",
    "build_runtime_adapter",
    "runtime_ids",
]
