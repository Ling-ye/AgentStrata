"""Owner 全局视图：扫描所有 per-user 工作区，输出只读摘要。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from chatcopilot.core.workspace_runtime.cleanup import collect_files
from chatcopilot.core.workspace_runtime.identity import read_workspace_identity
from chatcopilot.core.workspace_runtime.model import (
    ATTACHMENTS_RELPATH,
    IDENTITY_FILENAME,
    MEMORY_FILENAME,
    TRANSCRIPTS_DIRNAME,
)
from chatcopilot.core.workspace_runtime.resolver import resolve_workspace_root


@dataclass(frozen=True)
class WorkspaceInventory:
    """Owner 管理视图中的单个工作区只读摘要。"""

    root: Path
    relative_path: str
    chat_kind: Optional[str]
    chat_id: Optional[str]
    user_id: Optional[str]
    user_name: Optional[str]
    layout: str
    stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    total_files: int = 0
    total_bytes: int = 0
    latest_mtime: Optional[float] = None


def list_workspace_inventories(root: Optional[Path] = None) -> list[WorkspaceInventory]:
    """扫描所有已知 per-user 工作区并返回只读摘要。"""
    base = (root or resolve_workspace_root()).expanduser().resolve()
    if not base.is_dir():
        return []

    workspaces: list[WorkspaceInventory] = []
    seen: set[Path] = set()

    def add(path: Path, chat_kind: Optional[str], chat_id: Optional[str], user_id: Optional[str], layout: str) -> None:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_dir():
            return
        seen.add(resolved)
        workspaces.append(
            _build_inventory(
                root=resolved,
                base=base,
                chat_kind=chat_kind,
                chat_id=chat_id,
                user_id=user_id,
                layout=layout,
            )
        )

    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith("p2p_"):
            add(entry, "p2p", None, name[len("p2p_"):], "p2p_user")
            continue
        if name.startswith("group_"):
            chat_id = name[len("group_"):]
            user_children = [p for p in entry.iterdir() if p.is_dir() and p.name.startswith("user_")]
            for child in sorted(user_children, key=lambda p: p.name):
                add(child, "group", chat_id, child.name[len("user_"):], "group_user")
            shared = entry / "shared"
            if shared.is_dir():
                add(shared, "group", chat_id, None, "group_shared")
            if not user_children and not shared.is_dir() and _looks_like_workspace(entry):
                add(entry, "group", chat_id, None, "legacy_chat")
            continue
        if name == "default":
            add(entry, None, None, None, "default")
            continue
        if _looks_like_workspace(entry):
            kind, sep, chat_id = name.partition("_")
            add(entry, kind if sep else None, chat_id if sep else None, None, "legacy_chat")

    workspaces.sort(key=lambda item: item.latest_mtime or 0, reverse=True)
    return workspaces


def _looks_like_workspace(path: Path) -> bool:
    markers = (
        "downloads",
        "results",
        "uploads",
        "transcripts",
        "jobs",
        MEMORY_FILENAME,
    )
    if path.joinpath(*ATTACHMENTS_RELPATH).exists():
        return True
    return any((path / marker).exists() for marker in markers)


def _build_inventory(
    *,
    root: Path,
    base: Path,
    chat_kind: Optional[str],
    chat_id: Optional[str],
    user_id: Optional[str],
    layout: str,
) -> WorkspaceInventory:
    identity = read_workspace_identity(root)
    chat_kind = identity.get("chat_kind") or chat_kind
    chat_id = identity.get("chat_id") or chat_id
    user_id = identity.get("user_id") or user_id
    user_name = identity.get("user_name") or None

    stats: Dict[str, Dict[str, Any]] = {}
    total_files = 0
    total_bytes = 0
    latest_mtime: Optional[float] = None

    targets = {
        "attachments": root.joinpath(*ATTACHMENTS_RELPATH),
        "downloads": root / "downloads",
        "results": root / "results",
        "uploads": root / "uploads",
        "transcripts": root / TRANSCRIPTS_DIRNAME,
        "jobs": root / "jobs",
        "memory": root / MEMORY_FILENAME,
        "identity": root / IDENTITY_FILENAME,
    }
    for name, target in targets.items():
        item = _storage_stats(target)
        stats[name] = item
        total_files += int(item["files"])
        total_bytes += int(item["bytes"])
        item_mtime = item.get("latest_mtime")
        if isinstance(item_mtime, (int, float)):
            latest_mtime = item_mtime if latest_mtime is None else max(latest_mtime, item_mtime)

    try:
        relative_path = str(root.relative_to(base))
    except ValueError:
        relative_path = str(root)

    return WorkspaceInventory(
        root=root,
        relative_path=relative_path,
        chat_kind=chat_kind,
        chat_id=chat_id,
        user_id=user_id,
        user_name=user_name,
        layout=layout,
        stats=stats,
        total_files=total_files,
        total_bytes=total_bytes,
        latest_mtime=latest_mtime,
    )


def _storage_stats(target: Path) -> Dict[str, Any]:
    if target.is_file():
        try:
            stat = target.stat()
        except OSError:
            return {"exists": False, "files": 0, "bytes": 0, "latest_mtime": None}
        return {
            "exists": True,
            "files": 1,
            "bytes": stat.st_size,
            "latest_mtime": stat.st_mtime,
        }

    entries, total = collect_files(target)
    latest = max((mtime for _path, _size, mtime in entries), default=None)
    return {
        "exists": target.is_dir(),
        "files": len(entries),
        "bytes": total,
        "latest_mtime": latest,
    }


__all__ = ["WorkspaceInventory", "list_workspace_inventories"]
