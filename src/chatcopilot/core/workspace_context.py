"""Workspace service context shared by agent tools and external tools."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator, Protocol


class WorkspaceService(Protocol):
    def resolve_workspace(self, *, create: bool = True) -> Any: ...
    def resolve_workspace_root(self, workspace: Any = None) -> Path: ...
    def cleanup_workspace(self, workspace: Any) -> None: ...
    def describe_workspace(self, workspace: Any) -> str: ...
    def list_workspace_inventories(self, root: Path) -> list[Any]: ...


_CURRENT_WORKSPACE_SERVICE: ContextVar[WorkspaceService | None] = ContextVar(
    "chatcopilot_workspace_service",
    default=None,
)


def set_current_workspace_service(service: WorkspaceService | None) -> Token[WorkspaceService | None]:
    return _CURRENT_WORKSPACE_SERVICE.set(service)


def reset_current_workspace_service(token: Token[WorkspaceService | None]) -> None:
    _CURRENT_WORKSPACE_SERVICE.reset(token)


def get_current_workspace_service() -> WorkspaceService:
    service = _CURRENT_WORKSPACE_SERVICE.get()
    if service is None:
        raise RuntimeError("当前会话未注入 workspace service，无法执行 workspace 工具")
    return service


@contextmanager
def bind_workspace_service(service: WorkspaceService | None) -> Iterator[None]:
    token = set_current_workspace_service(service)
    try:
        yield
    finally:
        reset_current_workspace_service(token)


def resolve_workspace(*, create: bool = True) -> Any:
    return get_current_workspace_service().resolve_workspace(create=create)


def resolve_workspace_root(workspace: Any = None) -> Path:
    return get_current_workspace_service().resolve_workspace_root(workspace)


def cleanup_workspace(workspace: Any) -> None:
    get_current_workspace_service().cleanup_workspace(workspace)


def describe_workspace(workspace: Any) -> str:
    return get_current_workspace_service().describe_workspace(workspace)


def list_workspace_inventories(root: Path) -> list[Any]:
    return get_current_workspace_service().list_workspace_inventories(root)


__all__ = [
    "WorkspaceService",
    "bind_workspace_service",
    "cleanup_workspace",
    "describe_workspace",
    "get_current_workspace_service",
    "list_workspace_inventories",
    "reset_current_workspace_service",
    "resolve_workspace",
    "resolve_workspace_root",
    "set_current_workspace_service",
]
