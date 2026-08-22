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
    """Bind workspace paths and isolated backend state for the current actor."""

    backend_state_root: Path | None = None
    isolate_backend_state = False
    if workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED:
        protected_root = workspace.root.parent / ".conversation-state"
        if protected_root.is_symlink():
            raise RuntimeError("group conversation state directory must not be a symlink")
        protected_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        protected_root.chmod(0o700)
        backend_sessions_root = protected_root / "backend-sessions"
        if backend_sessions_root.is_symlink():
            raise RuntimeError("group backend state directory must not be a symlink")
        backend_sessions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        backend_sessions_root.chmod(0o700)
        if not workspace.user_id:
            raise RuntimeError("group backend state requires a stable actor identity")
        actor_digest = hashlib.sha256(f"qq\0{workspace.user_id}".encode("utf-8")).hexdigest()
        backend_state_root = backend_sessions_root / actor_digest
        if backend_state_root.is_symlink():
            raise RuntimeError("group actor backend state directory must not be a symlink")
        backend_state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        backend_state_root.chmod(0o700)
        isolate_backend_state = True
    return MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=resolve_workspace_root(workspace),
        backend_state_root=backend_state_root,
        isolate_backend_state=isolate_backend_state,
        platform_type=platform_type,
    )


__all__ = ["build_workspace_service"]
