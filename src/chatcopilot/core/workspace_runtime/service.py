"""WorkspaceService implementation backed by middleware workspace resolver."""
from __future__ import annotations

from pathlib import Path

from chatcopilot.core.workspace_runtime.cleanup import cleanup_workspace
from chatcopilot.core.workspace_runtime.inventory import (
    WorkspaceInventory,
    list_workspace_inventories,
)
from chatcopilot.core.workspace_runtime.model import Workspace, describe_workspace
from chatcopilot.core.workspace_runtime.resolver import (
    resolve_workspace,
    resolve_workspace_root,
)


class MiddlewareWorkspaceService:
    """Concrete workspace service injected into Agent tool execution."""

    def resolve_workspace(self, *, create: bool = True) -> Workspace:
        return resolve_workspace(create=create)

    def resolve_workspace_root(self, workspace: Workspace | None = None) -> Path:
        return resolve_workspace_root(workspace)

    def cleanup_workspace(self, workspace: Workspace) -> None:
        cleanup_workspace(workspace)

    def describe_workspace(self, workspace: Workspace) -> str:
        return describe_workspace(workspace)

    def list_workspace_inventories(self, root: Path) -> list[WorkspaceInventory]:
        return list_workspace_inventories(root)


__all__ = ["MiddlewareWorkspaceService"]
