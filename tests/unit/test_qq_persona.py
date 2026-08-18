"""QQ PromptAssembler assembly tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatcopilot.botspec.skills import SkillIndexEntry
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
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


def _fake_group_workspace() -> Workspace:
    tmp = (
        Path(tempfile.gettempdir())
        / "chatcopilot-qq-persona-group-test"
        / "shared"
    )
    tmp.mkdir(parents=True, exist_ok=True)
    return Workspace(
        root=tmp,
        chat_kind="group",
        chat_id="group-test",
        user_id="qq_owner_test",
        user_name="测试 Owner",
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
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

    def test_restricted_member_prompt_hides_model_and_internal_catalog(self) -> None:
        skills = (
            SkillIndexEntry(
                id="internal-playbook",
                name="Internal Playbook",
                description="internal",
                body_path=Path("/tmp/internal.md"),
            ),
        )
        text = build_system_prompt(
            _fake_workspace(),
            role=Role.USER,
            bot_system_prompt="x",
            capability_prompt_fragments=("internal capability",),
            skill_index=skills,
            llm_model="private-model",
            owner_only_project_access=True,
        )

        self.assertNotIn("private-model", text)
        self.assertNotIn("当前 LLM 模型", text)
        self.assertNotIn("internal capability", text)
        self.assertNotIn("internal-playbook", text)

    def test_restricted_owner_prompt_keeps_authorized_internal_catalog(self) -> None:
        skills = (
            SkillIndexEntry(
                id="internal-playbook",
                name="Internal Playbook",
                description="internal",
                body_path=Path("/tmp/internal.md"),
            ),
        )
        text = build_system_prompt(
            _fake_workspace(),
            role=Role.OWNER,
            bot_system_prompt="x",
            capability_prompt_fragments=("internal capability",),
            skill_index=skills,
            llm_model="private-model",
            owner_only_project_access=True,
        )

        self.assertIn("private-model", text)
        self.assertIn("internal capability", text)
        self.assertIn("internal-playbook", text)

    def test_restricted_owner_group_prompt_keeps_owner_catalog(self) -> None:
        skills = (
            SkillIndexEntry(
                id="internal-playbook",
                name="Internal Playbook",
                description="internal",
                body_path=Path("/tmp/internal.md"),
            ),
        )
        text = build_system_prompt(
            _fake_group_workspace(),
            role=Role.OWNER,
            bot_system_prompt="x",
            bot_refusal_prompt="restricted refusal",
            capability_prompt_fragments=("internal capability",),
            skill_index=skills,
            llm_model="private-model",
            owner_only_project_access=True,
        )

        self.assertIn("private-model", text)
        self.assertIn("internal capability", text)
        self.assertIn("internal-playbook", text)
        self.assertNotIn("restricted refusal", text)

    def test_owner_group_prompt_treats_style_persona_as_normal_mutation(self) -> None:
        bot_prompt = Path(
            "bots/lingye-copilot-qq/prompts/persona.md"
        ).read_text(encoding="utf-8")
        owner_prompt = Path(
            "bots/lingye-copilot-qq/prompts/roles/owner.md"
        ).read_text(encoding="utf-8")

        text = build_system_prompt(
            _fake_group_workspace(),
            role=Role.OWNER,
            bot_system_prompt=bot_prompt,
            role_prompts={"owner": owner_prompt},
            owner_only_project_access=True,
        )

        self.assertIn("不要把“不能逐字复刻或冒充”错误扩大成“不能设置人设”", text)
        self.assertIn("persona_set", text)
        self.assertIn("群聊未指定 scope 时默认 `group`", text)
        self.assertIn("群共享本身不授予权限", text)
        self.assertIn("仍按 Owner 角色执行", text)

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
