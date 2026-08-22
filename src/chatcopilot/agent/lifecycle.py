"""Process-local lifecycle intent propagation for nested agent sessions."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Callable, Iterator

from chatcopilot.contracts.agent import DeferredLifecycleIntent

LifecycleIntentCollector = Callable[[DeferredLifecycleIntent], None]
LifecycleIntentCollectorToken = contextvars.Token[LifecycleIntentCollector | None]

_CURRENT_COLLECTOR: contextvars.ContextVar[LifecycleIntentCollector | None] = (
    contextvars.ContextVar("chatcopilot_lifecycle_intent_collector", default=None)
)


@contextmanager
def bind_lifecycle_intent_collector(
    collector: LifecycleIntentCollector,
) -> Iterator[None]:
    token = _CURRENT_COLLECTOR.set(collector)
    try:
        yield
    finally:
        _CURRENT_COLLECTOR.reset(token)


def defer_lifecycle_intent(intent: DeferredLifecycleIntent) -> bool:
    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        return False
    collector(intent)
    return True


def set_lifecycle_intent_collector(
    collector: LifecycleIntentCollector,
) -> LifecycleIntentCollectorToken:
    return _CURRENT_COLLECTOR.set(collector)


def reset_lifecycle_intent_collector(
    token: LifecycleIntentCollectorToken,
) -> None:
    _CURRENT_COLLECTOR.reset(token)


__all__ = [
    "LifecycleIntentCollector",
    "LifecycleIntentCollectorToken",
    "bind_lifecycle_intent_collector",
    "defer_lifecycle_intent",
    "reset_lifecycle_intent_collector",
    "set_lifecycle_intent_collector",
]
