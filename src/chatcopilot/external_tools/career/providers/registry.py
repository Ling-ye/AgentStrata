"""Optional provider registry for generic career intelligence."""
from __future__ import annotations

from chatcopilot.external_tools.career.providers.base import CareerSourceProvider


_PROVIDERS: tuple[type[CareerSourceProvider], ...] = ()


def get_providers() -> tuple[CareerSourceProvider, ...]:
    return tuple(provider() for provider in _PROVIDERS)


__all__ = ["get_providers"]
