"""Workspace dataclass + 路径常量 + 显示 helper。"""
from __future__ import annotations

from dataclasses import dataclass

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

MEMORY_INITIAL_TEMPLATE = """# Memory

> 长期记忆。仅在用户告知**可复用**的偏好、默认参数、数据源、决策时写入。
> 临时对话内容不要写进来；体积过大请用 clear_memory 重置。

## facts
<!-- 用户告知的可复用事实，如默认阈值、习惯口径、常用数据源 URL -->

## decisions
<!-- 重要的处理决策与工作流偏好，如"先趋势再 diff" -->
"""


@dataclass(frozen=True)
class Workspace(WorkspaceView):
    """Conversation workspace plus the current turn's actor metadata."""

    def ensure(self) -> "Workspace":
        """创建工作目录及其所有子目录，并初始化 MEMORY.md / IDENTITY.json。"""
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
        # A shared group root is member-writable data, never host control state.
        # Do not follow a member-created MEMORY.md symlink or create identity
        # metadata there; the authoritative conversation/actor records live in
        # the protected sibling `.conversation-state` directory.
        if self.scope != WORKSPACE_SCOPE_GROUP_SHARED:
            mem = self.memory_file
            if not mem.exists():
                try:
                    mem.write_text(MEMORY_INITIAL_TEMPLATE, encoding="utf-8")
                except OSError:
                    pass
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
