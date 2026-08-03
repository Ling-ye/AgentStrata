"""Skill manifest 解析 + frontmatter 抽取 + 错误路径覆盖。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec.skills import (
    SkillIndexEntry,
    SkillManifestError,
    load_skill_index,
    read_skill_body,
    render_skill_index_section,
)


def _write_skill(base: Path, skill_id: str, *, name: str, description: str, body: str) -> Path:
    skill_dir = base / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_path


def _write_manifest(base: Path, text: str) -> Path:
    manifest_path = base / "manifest.yaml"
    manifest_path.write_text(text, encoding="utf-8")
    return manifest_path


class SkillManifestLoaderTests(unittest.TestCase):
    def test_load_skill_index_parses_frontmatter_in_manifest_order(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_skill(base, "alpha", name="Alpha Skill", description="阿尔法用途。Use when alpha.", body="alpha body")
            _write_skill(base, "beta", name="Beta Skill", description="贝塔用途。Use when beta.", body="beta body")
            manifest = _write_manifest(
                base,
                "skills:\n  - id: beta\n  - id: alpha\n",
            )

            entries = load_skill_index(manifest)

        self.assertEqual([e.id for e in entries], ["beta", "alpha"])
        self.assertEqual(entries[0].name, "Beta Skill")
        self.assertIn("贝塔", entries[0].description)
        self.assertTrue(entries[1].body_path.name == "SKILL.md")

    def test_disabled_skill_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_skill(base, "alpha", name="A", description="x. Use when a.", body="a")
            _write_skill(base, "beta", name="B", description="y. Use when b.", body="b")
            manifest = _write_manifest(
                base,
                "skills:\n  - id: alpha\n  - id: beta\n    enabled: false\n",
            )

            entries = load_skill_index(manifest)

        self.assertEqual([e.id for e in entries], ["alpha"])

    def test_missing_skill_md_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest = _write_manifest(base, "skills:\n  - id: ghost\n")
            with self.assertRaises(SkillManifestError) as cm:
                load_skill_index(manifest)
            self.assertIn("ghost", str(cm.exception))

    def test_missing_frontmatter_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "noheader").mkdir()
            (base / "noheader" / "SKILL.md").write_text("# title\n\nbody only", encoding="utf-8")
            manifest = _write_manifest(base, "skills:\n  - id: noheader\n")
            with self.assertRaises(SkillManifestError) as cm:
                load_skill_index(manifest)
            self.assertIn("frontmatter", str(cm.exception))

    def test_duplicate_id_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_skill(base, "alpha", name="A", description="x. Use when a.", body="a")
            manifest = _write_manifest(base, "skills:\n  - id: alpha\n  - id: alpha\n")
            with self.assertRaises(SkillManifestError) as cm:
                load_skill_index(manifest)
            self.assertIn("alpha", str(cm.exception))

    def test_read_skill_body_strips_frontmatter(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            skill_path = _write_skill(
                base,
                "alpha",
                name="A",
                description="x. Use when a.",
                body="# Heading\n\nBody content.",
            )
            entry = SkillIndexEntry(
                id="alpha", name="A", description="x", body_path=skill_path
            )
            body = read_skill_body(entry)

        self.assertIn("# Heading", body)
        self.assertNotIn("---", body.splitlines()[0] if body.splitlines() else "")
        self.assertNotIn("description:", body)

    def test_render_skill_index_section_empty(self) -> None:
        self.assertEqual(render_skill_index_section(()), "")

    def test_render_skill_index_section_lists_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = _write_skill(base, "alpha", name="Alpha", description="阿尔法用途。Use when α.", body="x")
            entries = (SkillIndexEntry(id="alpha", name="Alpha", description="阿尔法用途。Use when α.", body_path=p),)
            section = render_skill_index_section(entries)

        self.assertIn("## 可用 Skills", section)
        self.assertIn("`alpha`", section)
        self.assertIn("**Alpha**", section)
        self.assertIn("Use when", section)


if __name__ == "__main__":
    unittest.main()
