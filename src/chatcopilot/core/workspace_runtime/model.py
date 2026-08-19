"""Workspace dataclass + 路径常量 + 显示 helper。"""
from __future__ import annotations

from dataclasses import dataclass

from chatcopilot.contracts.persistent_state import MEMORY_INITIAL_TEMPLATE

from chatcopilot.contracts.workspace import (
    ATTACHMENTS_RELPATH,
    IDENTITY_FILENAME,
    MEMORY_FILENAME,
    TRANSCRIPTS_DIRNAME,
    WORKSPACE_SUBDIRS,
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
    WorkspaceView,
    describe_workspace_view,
    normalize_chat_kind,
)

# 工作区可被 list_workspace 直接列举的逻辑子目录名（attachments 走单独 property）。
_SUBDIRS = WORKSPACE_SUBDIRS

@dataclass(frozen=True)
class Workspace(WorkspaceView):
    """Conversation workspace plus the current turn's actor metadata."""

    def ensure(self) -> "Workspace":
        """创建成员可见工作目录；权威 persona/memory 位于保护状态域。"""
        data_subdirs = [
            self.root,
            self.downloads,
            self.results,
            self.uploads,
            self.attachments,
        ]
        if self.scope != WORKSPACE_SCOPE_GROUP_SHARED:
            data_subdirs.extend((self.tasks, self.transcripts))
        for sub in data_subdirs:
            sub.mkdir(parents=True, exist_ok=True)
        # MEMORY.md is now only a legacy migration locator.  Never create a new
        # authoritative memory file inside a member-writable workspace.
        # 避免循环 import：identity 持久化由 identity 模块负责
        from chatcopilot.core.workspace_runtime.identity import (
            persist_workspace_identity,
        )

        if self.scope != WORKSPACE_SCOPE_GROUP_SHARED:
            persist_workspace_identity(self)
        return self


def describe_workspace(ws: Workspace) -> str:
    """返回供 LLM 看的一行摘要（写进 tool 响应里）。"""
    return describe_workspace_view(ws)


__all__ = [
    "ATTACHMENTS_RELPATH",
    "IDENTITY_FILENAME",
    "MEMORY_FILENAME",
    "MEMORY_INITIAL_TEMPLATE",
    "TRANSCRIPTS_DIRNAME",
    "WORKSPACE_SCOPE_ACTOR",
    "WORKSPACE_SCOPE_GROUP_SHARED",
    "Workspace",
    "describe_workspace",
    "normalize_chat_kind",
]
