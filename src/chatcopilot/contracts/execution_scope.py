"""Host-bound filesystem resources, independent of Backend and role policy."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


RUNTIME_ACCESS_POLICY_VERSION = "runtime-access-v3"


@dataclass(frozen=True)
class CommandTimeouts:
    timeout_default: int = 60
    timeout_max: int = 300

    def __post_init__(self) -> None:
        for name in ("timeout_default", "timeout_max"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"command {name} must be a positive integer")
        object.__setattr__(self, "timeout_default", min(self.timeout_default, self.timeout_max))


@dataclass(frozen=True)
class ExecutionScope:
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...] = ()
    project_roots: tuple[Path, ...] = ()
    protected_roots: tuple[Path, ...] = ()
    hidden_roots: tuple[Path, ...] = ()
    native_write: bool = False
    policy_version: str = RUNTIME_ACCESS_POLICY_VERSION
    command_timeouts: CommandTimeouts = field(default_factory=CommandTimeouts)

    def permits(self, path: Path, *, write: bool = False) -> bool:
        resolved = path.resolve()
        if any(resolved == root or root in resolved.parents for root in self.hidden_roots):
            return False
        if write and any(
            resolved == root or root in resolved.parents for root in self.protected_roots
        ):
            return False
        roots = self.writable_roots if write else self.readable_roots
        return any(resolved == root or root in resolved.parents for root in roots)


_SCOPE: ContextVar[ExecutionScope | None] = ContextVar("execution_scope", default=None)


def current_execution_scope() -> ExecutionScope | None:
    return _SCOPE.get()


@contextmanager
def bind_execution_scope(scope: ExecutionScope | None) -> Iterator[None]:
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)
