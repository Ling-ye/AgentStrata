"""Task-local development policy shared by agent and external-tool layers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
from typing import Iterator


_READ_ONLY_MARKERS = frozenset({"none", "read-only", "readonly", "no-write"})
_NON_FILE_SCOPE_PREFIXES = ("mcp:", "remote:")
_SCOPE_SPLIT_RE = re.compile(r"[,;\n]+")


@dataclass(frozen=True)
class DevelopmentTaskScope:
    """Effective mutation policy for one delegated development task.

    ``allowed_paths`` is an additional restriction intersected with the bot-level
    dev configuration.  An empty tuple means that file mutation is denied.
    """

    allowed_paths: tuple[str, ...] = ()
    shell_profile: str = "validation"
    task_label: str = ""


_CURRENT_DEVELOPMENT_TASK_SCOPE: ContextVar[DevelopmentTaskScope | None] = ContextVar(
    "chatcopilot_development_task_scope",
    default=None,
)


def parse_write_scope(value: str | None) -> tuple[str, ...]:
    """Normalize a task-pack ``write_scope`` into repository-relative patterns."""

    raw = str(value or "").strip()
    if not raw or raw.lower() in _READ_ONLY_MARKERS:
        return ()

    patterns: list[str] = []
    for entry in _SCOPE_SPLIT_RE.split(raw):
        pattern = entry.strip().lstrip("- ").replace("\\", "/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.startswith("/") or any(part == ".." for part in pattern.split("/")):
            raise ValueError(f"write_scope must use repository-relative paths: {entry.strip()}")
        if pattern.lower().startswith(_NON_FILE_SCOPE_PREFIXES):
            continue
        patterns.append(pattern)
    return tuple(dict.fromkeys(patterns))


def current_development_task_scope() -> DevelopmentTaskScope | None:
    """Return the active delegated-task policy, if execution is delegated."""

    return _CURRENT_DEVELOPMENT_TASK_SCOPE.get()


@contextmanager
def development_task_scope(scope: DevelopmentTaskScope) -> Iterator[None]:
    """Install a delegated-task policy for the current execution context."""

    token = _CURRENT_DEVELOPMENT_TASK_SCOPE.set(scope)
    try:
        yield
    finally:
        _CURRENT_DEVELOPMENT_TASK_SCOPE.reset(token)


__all__ = [
    "DevelopmentTaskScope",
    "current_development_task_scope",
    "development_task_scope",
    "parse_write_scope",
]
