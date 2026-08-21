"""``filesystem.windows.read`` cross-tool policy manifest.

Declares structured policies selected by the PromptPlan builder when a bot
includes ``filesystem.windows.read`` in ``tools.packs``. The fragments
deliberately stay short and steer the LLM toward project-aware tools first.
"""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPolicy, tool_pack_policies


def build_windows_fs_read_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies(
        "filesystem.windows.read",
            "需要直接读取 Windows 文件系统（包括 WSL 下的 /mnt/f/...）上任意被允许路径的文件时，"
            "使用 win_read_file / win_grep / win_glob 工具。它们以绝对路径输入，并受 windows_fs 全局白名单约束。",
            "如果目标文件位于已注册的 Unity 工程内，"
            "请优先使用 unity_codebase 工具组（unity_project_read / unity_project_search / unity_find_csharp_symbol），"
            "它们带工程注册表和 C# 语义模式，定位更精准；win_* 工具用于工程之外的散落文件或临时探查。",
    )


TOOL_PACK_POLICY_BUILDERS = {
    "filesystem.windows.read": build_windows_fs_read_pack,
}


__all__ = ["TOOL_PACK_POLICY_BUILDERS", "build_windows_fs_read_pack"]
