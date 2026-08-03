"""read_bot_skill 工具：命中返回 body、未命中含可用 id、空注册表报错。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.agent.tools.builtin import skill_tools as bot_skills
from chatcopilot.botspec.skills import SkillIndexEntry


def _write_skill_body(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: alpha\ndescription: 测试用 skill。Use when testing.\n---\n\n# Alpha\n\nbody-content-line\n",
        encoding="utf-8",
    )


class ReadBotSkillToolTests(unittest.TestCase):
    def tearDown(self) -> None:
        bot_skills.set_skill_index(())

    def test_handler_returns_body_without_frontmatter(self) -> None:
        with TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "alpha" / "SKILL.md"
            _write_skill_body(skill_path)
            entry = SkillIndexEntry(
                id="alpha",
                name="alpha",
                description="测试用 skill。",
                body_path=skill_path,
            )
            bot_skills.set_skill_index((entry,))

            summary, outputs, _ = bot_skills._handler_read_bot_skill({"skill_id": "alpha"})

        self.assertIn("alpha", summary)
        self.assertIn("# Alpha", summary)
        self.assertIn("body-content-line", summary)
        self.assertNotIn("description: 测试用 skill", summary)
        self.assertEqual(outputs, [str(skill_path)])

    def test_handler_lists_available_when_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "alpha" / "SKILL.md"
            _write_skill_body(skill_path)
            entry = SkillIndexEntry(
                id="alpha", name="alpha", description="x", body_path=skill_path
            )
            bot_skills.set_skill_index((entry,))

            with self.assertRaises(ValueError) as cm:
                bot_skills._handler_read_bot_skill({"skill_id": "ghost"})

        self.assertIn("alpha", str(cm.exception))
        self.assertIn("ghost", str(cm.exception))

    def test_handler_raises_when_registry_empty(self) -> None:
        bot_skills.set_skill_index(())
        with self.assertRaises(ValueError) as cm:
            bot_skills._handler_read_bot_skill({"skill_id": "alpha"})
        self.assertIn("未注册", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
