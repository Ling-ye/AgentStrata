"""bots/lingye-copilot-qq end-to-end BotSpec smoke tests."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from chatcopilot.botspec import (
    assemble_runtime_context,
    load_botspec,
    validate_botspec,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOT_PATH = _REPO_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"


class LingyeBotSpecSmokeTests(unittest.TestCase):
    def test_bot_yaml_exists(self) -> None:
        self.assertTrue(_BOT_PATH.is_file(), msg=f"BotSpec file missing: {_BOT_PATH}")

    def test_validate_no_error(self) -> None:
        spec = load_botspec(_BOT_PATH)
        issues = validate_botspec(spec)
        errors = [issue for issue in issues if issue.level == "error"]
        self.assertEqual(
            errors,
            [],
            msg="lingye-copilot-qq BotSpec should not have errors: "
            + "; ".join(f"{i.field}: {i.message}" for i in errors),
        )

    def test_assemble_runtime_context_smoke(self) -> None:
        test_env = {
            "TAVILY_API_KEY": "tvly-test",
            "GITHUB_MCP_AUTHORIZATION": "Bearer github-test",
        }
        previous = {key: os.environ.get(key) for key in test_env}
        os.environ.update(test_env)
        try:
            spec = load_botspec(_BOT_PATH)
            runtime = assemble_runtime_context(spec)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(runtime.bot_id, "lingye-copilot-qq")
        self.assertEqual(runtime.platform_type, "qq")
        self.assertEqual(runtime.platform_adapter, "qq_acp")
        self.assertTrue(runtime.system_prompt, msg="system_prompt should not be empty")
        self.assertIn("Lingye的AI助手", runtime.system_prompt)
        self.assertIn("workspace.read_write", runtime.tool_packs)
        self.assertIn("memory.chat", runtime.tool_packs)
        self.assertNotIn("unity.codebase.read", runtime.tool_packs)
        self.assertIn("filesystem.windows.read", runtime.tool_packs)
        self.assertEqual(
            [server.id for server in runtime.mcp_servers],
            ["tavily", "searxng", "xiaohongshu", "playwright", "github", "taoke", "git"],
        )
        prefixes = {server.id: server.tool_prefix for server in runtime.mcp_servers}
        self.assertEqual(prefixes["tavily"], "web_")
        self.assertEqual(prefixes["searxng"], "sx_")
        self.assertEqual(
            [skill.id for skill in runtime.skills],
            ["ai-career-intelligence", "ai-jd-analysis"],
        )

    def test_owner_isolated_development_surface(self) -> None:
        spec = load_botspec(_BOT_PATH)
        self.assertIn("dev.code_tasks", spec.tools.packs)
        self.assertNotIn("dev.files", spec.tools.packs)
        self.assertNotIn("dev.shell", spec.tools.packs)
        self.assertNotIn("dev.lifecycle", spec.tools.packs)
        self.assertNotIn("codebase.read", spec.tools.packs)
        self.assertNotIn("codebase.change", spec.tools.packs)
        self.assertNotIn("web.fetch", spec.tools.packs)
        self.assertEqual(spec.agents.include, ())
        self.assertFalse(spec.agents.research_enabled)
        self.assertEqual(spec.agents.codex.owner_access, "worktree")
        self.assertEqual(spec.agents.codex.member_access, "workspace")
        self.assertEqual(spec.llm.code.allowed_roles, ("owner",))
        self.assertNotIn("developer", spec.agents.include)
        self.assertEqual(spec.agents.workflows, ())
