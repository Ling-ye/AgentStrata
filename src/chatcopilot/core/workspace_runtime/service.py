"""WorkspaceService implementation backed by middleware workspace resolver."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatcopilot.core.persistent_state import FilesystemPersistentConversationState

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


@dataclass
class MiddlewareWorkspaceService:
    """Concrete workspace service injected into Agent tool execution."""

    workspace: Workspace | None = None
    workspace_root: Path | None = None
    backend_state_root: Path | None = None
    isolate_backend_state: bool = False
    platform_type: str = "unknown"
    persistent_state: Any = None

    def resolve_workspace(self, *, create: bool = True) -> Workspace:
        if self.workspace is not None:
            return self.workspace.ensure() if create else self.workspace
        return resolve_workspace(create=create)

    def resolve_workspace_root(self, workspace: Workspace | None = None) -> Path:
        if self.workspace_root is not None:
            return self.workspace_root.expanduser().resolve()
        if self.workspace is not None:
            return resolve_workspace_root(self.workspace)
        return resolve_workspace_root(workspace)

    def cleanup_workspace(self, workspace: Workspace) -> None:
        cleanup_workspace(workspace)

    def resolve_backend_state_root(self) -> Path | None:
        if self.backend_state_root is None:
            return None
        return self.backend_state_root.expanduser().resolve()

    def requires_backend_state_isolation(self) -> bool:
        return self.isolate_backend_state

    def describe_workspace(self, workspace: Workspace) -> str:
        return describe_workspace(workspace)

    def list_workspace_inventories(self, root: Path) -> list[WorkspaceInventory]:
        return list_workspace_inventories(root)

    def resolve_persistent_state(self) -> Any:
        if self.persistent_state is None:
            workspace = self.resolve_workspace(create=True)
            self.persistent_state = FilesystemPersistentConversationState(
                workspace_root=self.resolve_workspace_root(workspace),
                workspace=workspace,
                platform=self.platform_type,
            )
        return self.persistent_state


__all__ = ["MiddlewareWorkspaceService"]
