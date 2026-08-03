"""提示词分层重构：BotSpec prompts 解析 + 框架默认/覆盖 装配测试。

覆盖两条链路：
- 配置层：``bots/<id>/prompts`` 的 persona/refusal/safety/memory/modes/roles 被
  loader 解析、被 runtime 解析成 ``BotRuntimeContext`` 的覆盖字段。
- 组合层：``middleware.acp.prompt_assembler.build_system_prompt`` 在 bot 未提供覆盖时回退到
  框架内置中性默认（``agent.context.builtin_prompts``），提供覆盖时优先用覆盖。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatcopilot.agent.context.builtin_prompts import (
    default_memory,
    default_role,
    default_safety,
)
from chatcopilot.botspec import assemble_runtime_context, load_botspec
from chatcopilot.middleware.access_control import AssistantMode, Role
from chatcopilot.middleware.runtime.workspace import Workspace
from chatcopilot.middleware.acp.prompt_assembler import build_system_prompt as _build_system_prompt


def build_system_prompt(workspace: Workspace, **kwargs) -> str:
    return _build_system_prompt(platform_type="feishu", workspace=workspace, **kwargs)


def _fake_workspace() -> Workspace:
    tmp = Path(tempfile.gettempdir()) / "chatcopilot-prompt-layering-test"
    tmp.mkdir(parents=True, exist_ok=True)
    return Workspace(
        root=tmp,
        chat_kind="p2p",
        chat_id=None,
        user_id="ou_test",
        user_name="测试用户",
    )


def _write_bot(root: Path) -> Path:
    """写出一个最小可校验的 feishu BotSpec（带分层 prompts），返回 bot.yaml 路径。"""
    prompts = root / "prompts"
    (prompts / "modes").mkdir(parents=True, exist_ok=True)
    (prompts / "roles").mkdir(parents=True, exist_ok=True)
    (prompts / "persona.md").write_text("# 我是测试机器人 PERSONA", encoding="utf-8")
    (prompts / "refusal.md").write_text("拒答口径 REFUSAL", encoding="utf-8")
    (prompts / "safety.md").write_text("自定义安全 SAFETY-OVERRIDE", encoding="utf-8")
    (prompts / "modes" / "performance.md").write_text("性能模式 PERF-MODE", encoding="utf-8")
    (prompts / "roles" / "owner.md").write_text("自定义 Owner OWNER-OVERRIDE", encoding="utf-8")
    bot_yaml = root / "bot.yaml"
    bot_yaml.write_text(
        "id: test-layering-bot\n"
        "display_name: Test Layering\n"
        "platform:\n"
        "  type: feishu\n"
        "  adapter: feishu_acp\n"
        "prompts:\n"
        "  persona: prompts/persona.md\n"
        "  refusal: prompts/refusal.md\n"
        "  safety: prompts/safety.md\n"
        "  modes:\n"
        "    performance: prompts/modes/performance.md\n"
        "  roles:\n"
        "    owner: prompts/roles/owner.md\n"
        "tools:\n"
        "  packs:\n"
        "    - workspace.read_write\n"
        "context:\n"
        "  memory_store:\n"
        "    namespace: test-layering-bot\n",
        encoding="utf-8",
    )
    return bot_yaml


class BotSpecPromptResolutionTests(unittest.TestCase):
    def test_runtime_resolves_layered_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot_yaml = _write_bot(Path(tmp))
            runtime = assemble_runtime_context(load_botspec(bot_yaml))

        self.assertIn("PERSONA", runtime.system_prompt)
        self.assertEqual(runtime.refusal_prompt, "拒答口径 REFUSAL")
        self.assertEqual(runtime.safety_prompt_override, "自定义安全 SAFETY-OVERRIDE")
        self.assertIsNone(runtime.memory_prompt_override)
        self.assertEqual(runtime.mode_prompt_overrides, {"performance": "性能模式 PERF-MODE"})
        self.assertEqual(runtime.role_prompt_overrides, {"owner": "自定义 Owner OWNER-OVERRIDE"})


class FeishuPersonaDefaultAndOverrideTests(unittest.TestCase):
    def test_falls_back_to_framework_defaults(self) -> None:
        text = build_system_prompt(_fake_workspace(), role=Role.USER)
        # 框架默认安全 / 记忆 / 角色片段应出现
        self.assertIn("通用安全与信息边界", text)
        self.assertIn("长期记忆", text)
        self.assertIn("当前用户权限：User", text)

    def test_runtime_model_is_rendered_in_session_context(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            role=Role.USER,
            llm_model="dashscope/deepseek-v4-pro",
        )
        self.assertIn("当前 LLM 模型", text)
        self.assertIn("dashscope/deepseek-v4-pro", text)

    def test_bot_overrides_take_precedence(self) -> None:
        text = build_system_prompt(
            _fake_workspace(),
            role=Role.OWNER,
            assistant_mode=AssistantMode.PERFORMANCE,
            mode_prompts={"performance": "MODE-PERF-OVERRIDE"},
            role_prompts={"owner": "ROLE-OWNER-OVERRIDE"},
            safety_prompt="SAFETY-OVERRIDE",
            memory_prompt="MEMORY-OVERRIDE",
        )
        self.assertIn("MODE-PERF-OVERRIDE", text)
        self.assertIn("ROLE-OWNER-OVERRIDE", text)
        self.assertIn("SAFETY-OVERRIDE", text)
        self.assertIn("MEMORY-OVERRIDE", text)
        # 覆盖生效后不应再出现框架默认对应内容
        self.assertNotIn(default_safety(), text)
        self.assertNotIn(default_memory(), text)
        self.assertNotIn(default_role("owner"), text)

    def test_unknown_role_falls_back_to_user_default(self) -> None:
        # role_prompts 只给了 owner，user 角色应回退到框架默认 user 片段
        text = build_system_prompt(
            _fake_workspace(),
            role=Role.USER,
            role_prompts={"owner": "ROLE-OWNER-OVERRIDE"},
        )
        self.assertIn("当前用户权限：User", text)
        self.assertNotIn("ROLE-OWNER-OVERRIDE", text)


if __name__ == "__main__":
    unittest.main()
