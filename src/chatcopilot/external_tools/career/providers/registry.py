"""Reviewed public provider registry for generic career intelligence."""
from __future__ import annotations

from chatcopilot.external_tools.career.providers.base import CareerSourceProvider
from chatcopilot.external_tools.career.providers.portal import (
    ByteDanceProvider,
    MiniMaxProvider,
)
from chatcopilot.external_tools.career.providers.tencent import TencentProvider


_PROVIDERS: tuple[type[CareerSourceProvider], ...] = (
    TencentProvider,
    ByteDanceProvider,
    MiniMaxProvider,
)


def get_providers() -> tuple[CareerSourceProvider, ...]:
    return tuple(provider() for provider in _PROVIDERS)


def find_provider(company: str) -> CareerSourceProvider | None:
    return next(
        (provider for provider in get_providers() if provider.matches_company(company)),
        None,
    )


def supported_company_aliases() -> dict[str, list[str]]:
    return {
        provider.company: list(dict.fromkeys((provider.company, *provider.aliases)))
        for provider in get_providers()
    }


__all__ = ["find_provider", "get_providers", "supported_company_aliases"]
