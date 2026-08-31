"""Build actor workspaces only from trusted host inputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat

from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
)
from chatcopilot.core.persistent_state import FilesystemPersistentConversationState
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService, Workspace


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_PRIVATE_MODE = 0o700


class WorkspaceAssemblyError(RuntimeError):
    """Path-free failure raised before actor state is materialized."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ActorWorkspaceBinding:
    """Exact data and protected-state roots bound to one Principal."""

    workspace: Workspace
    service: MiddlewareWorkspaceService
    backend_state_root: Path | None


@dataclass(frozen=True)
class ApplicationWorkspace(Workspace):
    """Gateway-native workspace whose attachments have no legacy bridge prefix."""

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"

    def ensure(self) -> ApplicationWorkspace:
        """Return the layout already validated and created by the application factory."""

        return self


def build_actor_workspace(
    *,
    workspace_root: Path,
    principal: Principal,
) -> ActorWorkspaceBinding:
    """Construct one workspace without consulting cwd or process identity variables."""

    root = _validate_trusted_root(workspace_root)
    platform = _identity_segment(principal.conversation.platform, field="platform")
    channel = _identity_segment(principal.channel, field="channel")
    if channel != platform:
        raise WorkspaceAssemblyError(
            "workspace_principal_channel_mismatch",
            "The Principal channel does not match its conversation platform",
        )
    _identity_segment(principal.account_id, field="account_id")
    actor_id = _identity_segment(principal.user_id, field="user_id")
    chat_id = _identity_segment(principal.conversation.chat_id, field="chat_id")
    chat_kind = str(principal.conversation.chat_kind or "").strip().lower()

    backend_state_root: Path | None = None
    isolate_backend_state = False
    if chat_kind == "p2p":
        workspace_path = _ensure_data_directory(root, f"p2p_{actor_id}")
        scope = WORKSPACE_SCOPE_ACTOR
    elif chat_kind == "group":
        group_root = _ensure_data_directory(root, f"group_{chat_id}")
        workspace_path = _ensure_data_directory(group_root, "shared")
        state_root = _ensure_private_directory(group_root, ".conversation-state")
        sessions_root = _ensure_private_directory(state_root, "backend-sessions")
        digest = hashlib.sha256(
            "\0".join(
                (
                    platform,
                    principal.account_id,
                    chat_id,
                    actor_id,
                )
            ).encode("utf-8")
        ).hexdigest()
        backend_state_root = _ensure_private_directory(sessions_root, digest)
        isolate_backend_state = True
        scope = WORKSPACE_SCOPE_GROUP_SHARED
    else:
        raise WorkspaceAssemblyError(
            "workspace_conversation_kind_unsupported",
            "Actor workspaces support only p2p and group conversations",
        )

    workspace = ApplicationWorkspace(
        root=workspace_path,
        chat_kind=chat_kind,
        chat_id=chat_id,
        user_id=actor_id,
        user_name=None,
        scope=scope,
    )
    _prepare_workspace_layout(workspace)
    persistent_state = FilesystemPersistentConversationState(
        workspace_root=root,
        workspace=workspace,
        platform=platform,
    )
    service = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=root,
        backend_state_root=backend_state_root,
        isolate_backend_state=isolate_backend_state,
        platform_type=platform,
        persistent_state=persistent_state,
    )
    return ActorWorkspaceBinding(
        workspace=workspace,
        service=service,
        backend_state_root=backend_state_root,
    )


def _validate_trusted_root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or root != Path(os.path.normpath(os.fspath(root))):
        raise WorkspaceAssemblyError(
            "workspace_root_invalid",
            "The trusted workspace root must be an absolute normalized path",
        )
    try:
        current = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceAssemblyError(
            "workspace_root_unavailable",
            "The trusted workspace root is unavailable",
        ) from exc
    if (
        resolved != root
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) & 0o022
    ):
        raise WorkspaceAssemblyError(
            "workspace_root_unsafe",
            "The trusted workspace root has unsafe identity, ownership, or permissions",
        )
    return root


def _identity_segment(value: str, *, field: str) -> str:
    candidate = str(value or "").strip()
    if candidate in {".", ".."} or _IDENTITY_RE.fullmatch(candidate) is None:
        raise WorkspaceAssemblyError(
            "workspace_identity_invalid",
            f"The trusted {field} is not a safe stable identifier",
        )
    return candidate


def _ensure_data_directory(parent: Path, name: str) -> Path:
    path = parent / name
    created = _mkdir_one(path)
    current = _safe_lstat(path, code="workspace_storage_unsafe")
    if created:
        _set_mode(path, _PRIVATE_MODE, code="workspace_storage_unavailable")
        current = _safe_lstat(path, code="workspace_storage_unsafe")
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) & 0o022
    ):
        raise WorkspaceAssemblyError(
            "workspace_storage_unsafe",
            "Actor workspace storage has unsafe type, ownership, or permissions",
        )
    _assert_contained(parent, path, code="workspace_storage_unsafe")
    return path


def _ensure_private_directory(parent: Path, name: str) -> Path:
    path = parent / name
    created = _mkdir_one(path)
    current = _safe_lstat(path, code="backend_state_unsafe")
    if created:
        _set_mode(path, _PRIVATE_MODE, code="backend_state_unavailable")
        current = _safe_lstat(path, code="backend_state_unsafe")
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) != _PRIVATE_MODE
    ):
        raise WorkspaceAssemblyError(
            "backend_state_unsafe",
            "Protected backend state has unsafe type, ownership, or permissions",
        )
    _assert_contained(parent, path, code="backend_state_unsafe")
    return path


def _prepare_workspace_layout(workspace: Workspace) -> None:
    paths = [
        workspace.downloads,
        workspace.results,
        workspace.uploads,
        workspace.attachments,
    ]
    if workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
        paths.extend((workspace.tasks, workspace.transcripts))
    parent = workspace.root
    try:
        for path in paths:
            if path.parent != parent:
                _ensure_data_directory(parent, path.parent.name)
                parent = path.parent
            _ensure_data_directory(path.parent, path.name)
            parent = workspace.root
        workspace.ensure()
    except WorkspaceAssemblyError:
        raise
    except OSError as exc:
        raise WorkspaceAssemblyError(
            "workspace_storage_unavailable",
            "Actor workspace storage could not be initialized",
        ) from exc


def _mkdir_one(path: Path) -> bool:
    try:
        path.mkdir(mode=_PRIVATE_MODE)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        raise WorkspaceAssemblyError(
            "workspace_storage_unavailable",
            "Actor workspace storage could not be created",
        ) from exc


def _safe_lstat(path: Path, *, code: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise WorkspaceAssemblyError(code, "Actor workspace storage is unsafe") from exc


def _set_mode(path: Path, mode: int, *, code: str) -> None:
    try:
        path.chmod(mode)
    except OSError as exc:
        raise WorkspaceAssemblyError(code, "Actor workspace permissions could not be set") from exc


def _assert_contained(parent: Path, path: Path, *, code: str) -> None:
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_parent)
    except (OSError, ValueError) as exc:
        raise WorkspaceAssemblyError(code, "Actor workspace path escaped its parent") from exc
    if resolved_parent != parent or resolved_path != path:
        raise WorkspaceAssemblyError(code, "Actor workspace path contains a symbolic link")


__all__ = [
    "ActorWorkspaceBinding",
    "ApplicationWorkspace",
    "WorkspaceAssemblyError",
    "build_actor_workspace",
]
