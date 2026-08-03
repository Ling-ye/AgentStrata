"""BotSpec assembler 装配 skills 索引到 BotRuntimeContext 的端到端校验。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec import assemble_runtime_context, load_botspec, validate_botspec


_BOT_YAML = """\
id: skills-test-bot
display_name: SkillsTestBot

platform:
  type: feishu
  adapter: feishu_acp

prompts:
  persona: prompts/persona.md

tools:
  packs:
    - playbooks.reader
  hide: []

deploy:
  target: wsl2
  instance_id: skills-test-bot

context:
  playbooks:
    manifest: skills/manifest.yaml
"""

_SKILL_BODY = """\
---
name: alpha-skill
description: 一个测试 skill。Use when running unit tests.
---

# Alpha Skill

正文流程描述。
"""


def _scaffold_bot(root: Path) -> Path:
    bot_dir = root / "skills-test-bot"
    (bot_dir / "prompts").mkdir(parents=True)
    (bot_dir / "prompts" / "persona.md").write_text("# SkillsTestBot\n", encoding="utf-8")
    skills_dir = bot_dir / "skills"
    (skills_dir / "alpha-skill").mkdir(parents=True)
    (skills_dir / "alpha-skill" / "SKILL.md").write_text(_SKILL_BODY, encoding="utf-8")
    (skills_dir / "manifest.yaml").write_text(
        "skills:\n  - id: alpha-skill\n",
        encoding="utf-8",
    )
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_text(_BOT_YAML, encoding="utf-8")
    return bot_yaml


class AssemblerSkillsTests(unittest.TestCase):
    def test_runtime_context_includes_skill_index(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_yaml = _scaffold_bot(Path(tmp))
            spec = load_botspec(bot_yaml)
            issues = validate_botspec(spec)
            errors = [i for i in issues if i.level == "error"]
            self.assertEqual(errors, [], msg=f"unexpected validation errors: {errors}")

            ctx = assemble_runtime_context(spec)

            self.assertEqual(len(ctx.skills), 1)
            entry = ctx.skills[0]
            self.assertEqual(entry.id, "alpha-skill")
            self.assertEqual(entry.name, "alpha-skill")
            self.assertIn("Use when", entry.description)
            self.assertTrue(entry.body_path.is_file())

    def test_validate_reports_missing_skill_body(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_yaml = _scaffold_bot(Path(tmp))
            (Path(tmp) / "skills-test-bot" / "skills" / "manifest.yaml").write_text(
                "skills:\n  - id: ghost\n",
                encoding="utf-8",
            )
            spec = load_botspec(bot_yaml)
            issues = validate_botspec(spec)

        error_messages = [i.message for i in issues if i.level == "error"]
        self.assertTrue(any("ghost" in msg for msg in error_messages))


if __name__ == "__main__":
    unittest.main()
