"""Pure workspace path views shared by layers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ATTACHMENTS_RELPATH = (".cc-connect", "attachments")
MEMORY_FILENAME = "MEMORY.md"
TRANSCRIPTS_DIRNAME = "transcripts"
IDENTITY_FILENAME = "IDENTITY.json"
WORKSPACE_SUBDIRS = ("downloads", "results", "uploads", "jobs", "tasks")


def normalize_chat_kind(chat_kind: Optional[str], chat_id: Optional[str] = None) -> Optional[str]:
    raw_kind = (chat_kind or "").strip().lower().replace("-", "_")
    if "p2p" in raw_kind or raw_kind in {"private", "direct", "single"}:
        return "p2p"
    if "group" in raw_kind or raw_kind in {"chat", "room"}:
        return "group"
    if not raw_kind:
        return "p2p"
    return raw_kind or None


@dataclass(frozen=True)
class WorkspaceRef:
    """Stable workspace identity and root path."""

    root: Path
    chat_kind: Optional[str]
    chat_id: Optional[str]
    user_id: Optional[str] = None
    user_name: Optional[str] = None


@dataclass(frozen=True)
class WorkspaceView(WorkspaceRef):
    """Read-only logical path view for a chat/user workspace."""

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def attachments(self) -> Path:
        return self.root.joinpath(*ATTACHMENTS_RELPATH)

    @property
    def memory_file(self) -> Path:
        return self.root / MEMORY_FILENAME

    @property
    def transcripts(self) -> Path:
        return self.root / TRANSCRIPTS_DIRNAME

    @property
    def tasks(self) -> Path:
        return self.root / "tasks"

    def is_inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    def relpath(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)

    def resolve_subdir(self, name: str) -> Path:
        if name == "attachments":
            return self.attachments
        if name in WORKSPACE_SUBDIRS:
            return self.root / name
        raise ValueError(f"未知子目录: {name}")

    def resolve_relative(self, raw_path: str | Path) -> Path:
        parts = Path(raw_path).parts
        if parts and parts[0] in ("attachments", *WORKSPACE_SUBDIRS):
            base = self.resolve_subdir(parts[0])
            return base.joinpath(*parts[1:]) if len(parts) > 1 else base
        return self.root / Path(raw_path)


def describe_workspace_view(ws: WorkspaceView) -> str:
    parts = [f"workspace={ws.root}"]
    if ws.chat_id:
        parts.append(f"chat={ws.chat_kind or 'chat'}/{ws.chat_id}")
    if ws.user_id:
        parts.append(f"user={ws.user_id}")
    if ws.user_name:
        parts.append(f"name={ws.user_name}")
    return " ".join(parts)


__all__ = [
    "ATTACHMENTS_RELPATH",
    "IDENTITY_FILENAME",
    "MEMORY_FILENAME",
    "TRANSCRIPTS_DIRNAME",
    "WORKSPACE_SUBDIRS",
    "WorkspaceRef",
    "WorkspaceView",
    "describe_workspace_view",
    "normalize_chat_kind",
]
