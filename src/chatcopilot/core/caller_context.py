"""Caller identity context for agent tools (platform-neutral).

Middleware injects the caller's role hint (a plain string like ``"owner"``
or ``"user"``) via :func:`bind_caller_role`; privileged tools and other
scope-aware handlers read it through :func:`get_caller_role_hint` to
enforce per-scope permission without importing any middleware module.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

_CALLER_ROLE_HINT: ContextVar[str] = ContextVar(
    "chatcopilot_caller_role_hint",
    default="user",
)


def get_caller_role_hint() -> str:
    """Return the current caller's role hint (``"owner"`` / ``"admin"`` / ``"user"``)."""
    return _CALLER_ROLE_HINT.get()


def set_caller_role_hint(value: str) -> Token[str]:
    return _CALLER_ROLE_HINT.set(value)


def reset_caller_role_hint(token: Token[str]) -> None:
    _CALLER_ROLE_HINT.reset(token)


@contextmanager
def bind_caller_role(value: str) -> Iterator[None]:
    """Context manager that sets the caller role hint for the current scope."""
    token = set_caller_role_hint(value)
    try:
        yield
    finally:
        reset_caller_role_hint(token)


__all__ = [
    "bind_caller_role",
    "get_caller_role_hint",
    "reset_caller_role_hint",
    "set_caller_role_hint",
]
