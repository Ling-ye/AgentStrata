"""Agent-side compatibility wrapper for the shared workspace service context."""
from chatcopilot.core.workspace_context import (
    WorkspaceService,
    bind_workspace_service,
    cleanup_workspace,
    describe_workspace,
    get_current_workspace_service,
    list_workspace_inventories,
    reset_current_workspace_service,
    resolve_persistent_state,
    resolve_workspace,
    resolve_workspace_root,
    set_current_workspace_service,
)


__all__ = [
    "WorkspaceService",
    "bind_workspace_service",
    "cleanup_workspace",
    "describe_workspace",
    "get_current_workspace_service",
    "list_workspace_inventories",
    "reset_current_workspace_service",
    "resolve_persistent_state",
    "resolve_workspace",
    "resolve_workspace_root",
    "set_current_workspace_service",
]
