"""Structured policy for chat workspace delivery tools."""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPolicy, tool_pack_policies


def build_workspace_read_write_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies(
        "workspace.read_write",
        "用户要求把公网图片 URL 发到当前会话时，调用 send_image_urls_to_user；已有工作区文件"
        "使用 send_files_to_user。Markdown 图片或链接不是发送回执。只有工具成功回执后才能"
        "声称已发送；工具可见时不得声称当前没有图片发送能力。",
    )


TOOL_PACK_POLICY_BUILDERS = {
    "workspace.read_write": build_workspace_read_write_pack,
}


__all__ = ["TOOL_PACK_POLICY_BUILDERS", "build_workspace_read_write_pack"]
