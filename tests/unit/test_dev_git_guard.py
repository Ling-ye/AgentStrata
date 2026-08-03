from __future__ import annotations

import unittest

from chatcopilot.agent.mcp import client as mcp_client
from chatcopilot.botspec.mcp import McpServerConfig
from chatcopilot.external_tools.dev.git_guard import (
    AGENTIC_COMMIT_PREFIX,
    ensure_agentic_commit_message,
)


class DevGitGuardTests(unittest.TestCase):
    def test_ensure_agentic_commit_message_prepends_prefix(self) -> None:
        self.assertEqual(
            ensure_agentic_commit_message("重构搜索模块"),
            f"{AGENTIC_COMMIT_PREFIX} 重构搜索模块",
        )

    def test_ensure_agentic_commit_message_is_idempotent(self) -> None:
        prefixed = f"{AGENTIC_COMMIT_PREFIX} 重构搜索模块"
        self.assertEqual(ensure_agentic_commit_message(prefixed), prefixed)

    def test_ensure_agentic_commit_message_preserves_body_lines(self) -> None:
        self.assertEqual(
            ensure_agentic_commit_message("重构搜索模块\n\n- 拆分 router\n- 增加并发"),
            f"{AGENTIC_COMMIT_PREFIX} 重构搜索模块\n\n- 拆分 router\n- 增加并发",
        )

    def test_ensure_agentic_commit_message_empty(self) -> None:
        self.assertEqual(ensure_agentic_commit_message(""), AGENTIC_COMMIT_PREFIX)


class McpGitArgumentTests(unittest.TestCase):
    def test_git_commit_message_is_normalized(self) -> None:
        config = McpServerConfig(id="git", risk="write")

        normalized = mcp_client._normalize_mcp_tool_arguments(
            config,
            "git_commit",
            {"repo_path": "/tmp/repo", "message": "修复搜索超时"},
        )

        self.assertEqual(
            normalized["message"],
            f"{AGENTIC_COMMIT_PREFIX} 修复搜索超时",
        )

    def test_git_status_arguments_are_unchanged(self) -> None:
        config = McpServerConfig(id="git", risk="write")
        args = {"repo_path": "/tmp/repo"}

        normalized = mcp_client._normalize_mcp_tool_arguments(
            config,
            "git_status",
            args,
        )

        self.assertEqual(normalized, args)


if __name__ == "__main__":
    unittest.main()
