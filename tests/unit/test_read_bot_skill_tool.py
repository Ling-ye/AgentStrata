"""read_bot_skill 工具：命中返回 body、未命中含可用 id、空注册表报错。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.agent.tools.builtin import skill_tools as bot_skills
from chatcopilot.botspec.skills import SkillIndexEntry
from chatcopilot.contracts.tools import ToolContext


def _write_skill_body(path: Path, *, name: str = "alpha") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: 测试用 skill。Use when testing.\n---\n\n"
        f"# {name.title()}\n\nbody-content-{name}\n",
        encoding="utf-8",
    )


class ReadBotSkillToolTests(unittest.TestCase):
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
            handler = bot_skills.build_skill_provider((entry,)).packs[
                "playbooks.reader"
            ][0].handler
            result = handler({"skill_id": "alpha"}, ToolContext())

        self.assertIn("alpha", result.summary)
        self.assertIn("# Alpha", result.summary)
        self.assertIn("body-content-alpha", result.summary)
        self.assertNotIn("description: 测试用 skill", result.summary)
        self.assertEqual(result.outputs, [str(skill_path)])
        self.assertEqual(result.data["body_path"], str(skill_path))

    def test_handler_lists_available_when_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "alpha" / "SKILL.md"
            _write_skill_body(skill_path)
            entry = SkillIndexEntry(
                id="alpha", name="alpha", description="x", body_path=skill_path
            )
            handler = bot_skills.build_skill_provider((entry,)).packs[
                "playbooks.reader"
            ][0].handler
            with self.assertRaises(ValueError) as cm:
                handler({"skill_id": "ghost"}, ToolContext())

        self.assertIn("alpha", str(cm.exception))
        self.assertIn("ghost", str(cm.exception))

    def test_handler_raises_when_bound_index_is_empty(self) -> None:
        handler = bot_skills.build_skill_provider(()).packs["playbooks.reader"][
            0
        ].handler
        with self.assertRaises(ValueError) as cm:
            handler({"skill_id": "alpha"}, ToolContext())
        self.assertIn("未注册", str(cm.exception))

    def test_bound_providers_keep_bot_indexes_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first" / "SKILL.md"
            second_path = Path(tmp) / "second" / "SKILL.md"
            _write_skill_body(first_path, name="first")
            _write_skill_body(second_path, name="second")
            first = SkillIndexEntry(
                id="first",
                name="first",
                description="first",
                body_path=first_path,
            )
            second = SkillIndexEntry(
                id="second",
                name="second",
                description="second",
                body_path=second_path,
            )
            first_handler = bot_skills.build_skill_provider((first,)).packs[
                "playbooks.reader"
            ][0].handler
            second_handler = bot_skills.build_skill_provider((second,)).packs[
                "playbooks.reader"
            ][0].handler
            first_result = first_handler({"skill_id": "first"}, ToolContext())
            second_result = second_handler({"skill_id": "second"}, ToolContext())
            first_again = first_handler({"skill_id": "first"}, ToolContext())

        self.assertEqual(first_result.data["skill_id"], "first")
        self.assertIn("body-content-first", first_result.data["body"])
        self.assertEqual(second_result.data["skill_id"], "second")
        self.assertIn("body-content-second", second_result.data["body"])
        self.assertEqual(first_again.data, first_result.data)

if __name__ == "__main__":
    unittest.main()
