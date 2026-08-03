"""QQ PromptAssembler assembly tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatcopilot.botspec.skills import SkillIndexEntry
from chatcopilot.middleware.runtime.workspace import Workspace
from chatcopilot.middleware.acp.prompt_assembler import build_system_prompt as _build_system_prompt


def build_system_prompt(workspace: Workspace, **kwargs) -> str:
    return _build_system_prompt(platform_type="qq", workspace=workspace, **kwargs)


def _fake_workspace() -> Workspace:
    """Build a lightweight workspace for QQ persona prompt rendering."""
    tmp = Path(tempfile.gettempdir()) / "chatcopilot-qq-persona-test"
    tmp.mkdir(parents=True, exist_ok=True)
    return Workspace(
        root=tmp,
        chat_kind="p2p",
        chat_id=None,
        user_id="qq_user_test",
        user_name="测试用户",
    )


class QQPersonaTests(unittest.TestCase):
    def test_minimal_prompt_contains_session_header_and_safety(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="你是 Lingye的AI助手。",
        )
        self.assertIn("当前会话上下文", text)
        self.assertIn("QQ", text)
        self.assertIn("Lingye的AI助手", text)
        self.assertIn("通用安全与信息边界", text)

    def test_runtime_model_is_rendered_in_session_header(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="你是 Lingye的AI助手。",
            llm_model="deepseek-v4-pro",
        )
        self.assertIn("当前 LLM 模型", text)
        self.assertIn("deepseek-v4-pro", text)

    def test_role_and_assistant_mode_inputs_are_ignored(self) -> None:
        # QQ does not use the Feishu role matrix, so these inputs should not change output.
        a = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="bot prompt",
        )
        b = build_system_prompt(
            _fake_workspace(),
            role="OWNER",
            assistant_mode="GENERAL",
            bot_system_prompt="bot prompt",
        )
        self.assertEqual(a, b)

    def test_capability_fragments_render_as_bullet_list(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="x",
            capability_prompt_fragments=("能力 A", "能力 B"),
        )
        self.assertIn("当前可用能力", text)
        self.assertIn("- 能力 A", text)
        self.assertIn("- 能力 B", text)

    def test_skill_index_section_renders_when_entries_present(self) -> None:
        skills = (
            SkillIndexEntry(
                id="general-research-workflow",
                name="通用资料检索",
                description="通用公开资料检索与摘要流程。",
                body_path=Path("/tmp/dummy.md"),
            ),
        )
        text = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="x",
            skill_index=skills,
        )
        self.assertIn("可用 Skills", text)
        self.assertIn("general-research-workflow", text)

    def test_skill_index_empty_does_not_render_header(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="x",
        )
        self.assertNotIn("可用 Skills", text)

    def test_refusal_prompt_appended_when_present(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            bot_system_prompt="主提示",
            bot_refusal_prompt="拒答策略：保护敏感信息。",
        )
        self.assertIn("拒答策略：保护敏感信息", text)
        self.assertGreater(text.find("拒答策略"), text.find("主提示"))


if __name__ == "__main__":
    unittest.main()
