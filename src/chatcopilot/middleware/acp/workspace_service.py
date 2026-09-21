"""Construct actor-scoped workspace services for ACP sessions."""

from __future__ import annotations

import hashlib
from pathlib import Path

from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.workspace_runtime import (
    MiddlewareWorkspaceService,
    Workspace,
    resolve_workspace_root,
)


def build_workspace_service(
    workspace: Workspace,
    platform_type: str = "unknown",
) -> MiddlewareWorkspaceService:
    """Bind workspace paths and isolated runtime state for the current actor."""

    runtime_state_root: Path | None = None
    isolate_runtime_state = False
    if workspace.user_id:
        protected_root = (
            workspace.root.parent
            if workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
            else resolve_workspace_root(workspace)
        ) / ".conversation-state"
        if protected_root.is_symlink():
            raise RuntimeError("group conversation state directory must not be a symlink")
        protected_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        protected_root.chmod(0o700)
        runtime_sessions_root = protected_root / "runtime-sessions"
        if runtime_sessions_root.is_symlink():
            raise RuntimeError("group runtime state directory must not be a symlink")
        runtime_sessions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_sessions_root.chmod(0o700)
        if not workspace.user_id:
            raise RuntimeError("group runtime state requires a stable actor identity")
        actor_digest = hashlib.sha256(f"qq\0{workspace.user_id}".encode("utf-8")).hexdigest()
        runtime_state_root = runtime_sessions_root / actor_digest
        if runtime_state_root.is_symlink():
            raise RuntimeError("group actor runtime state directory must not be a symlink")
        runtime_state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_state_root.chmod(0o700)
        isolate_runtime_state = True
    return MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=resolve_workspace_root(workspace),
        runtime_state_root=runtime_state_root,
        isolate_runtime_state=isolate_runtime_state,
        platform_type=platform_type,
    )


__all__ = ["build_workspace_service"]
