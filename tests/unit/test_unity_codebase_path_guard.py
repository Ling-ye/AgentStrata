from __future__ import annotations

import unittest
from pathlib import Path

from chatcopilot.external_tools.unity_codebase.config import UnityProjectConfig
from chatcopilot.external_tools.unity_codebase.path_guard import (
    UnityPathAccessError,
    ensure_readable,
    ensure_searchable,
)


def _make_project(**overrides) -> UnityProjectConfig:
    defaults = dict(
        project_id="sample_game",
        display_name="SampleGame",
        root=Path("/mnt/f/SampleGame/v1d3/proj"),
        allow_globs=(
            "Assets/Scripts/**",
            "Assets/**/*.cs",
            "Packages/**",
            "ExternalTools/ForAI/**",
            "*.md",
        ),
        deny_globs=(
            "Library/**",
            "**/__pycache__/**",
            "Assets/**/*.png",
        ),
        allow_extensions=(".cs", ".lua", ".md", ".yaml"),
        skills={"path_book": ".claude/skills/path-book/scripts/path_book.py"},
        description="",
        max_read_bytes=1_048_576,
    )
    defaults.update(overrides)
    return UnityProjectConfig(**defaults)


class UnityPathGuardTests(unittest.TestCase):
    def test_rejects_absolute_rel_path(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "/abs/path.cs")

    def test_rejects_empty_rel_path(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "")

    def test_rejects_dotdot_escape(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "../outside.cs")

    def test_accepts_allowlisted_cs_under_scripts(self) -> None:
        project = _make_project()
        abs_path, norm_rel = ensure_readable(project, "Assets/Scripts/Mission/MissionPanel.cs")
        self.assertEqual(norm_rel, "Assets/Scripts/Mission/MissionPanel.cs")
        self.assertTrue(str(abs_path).replace("\\", "/").endswith("Assets/Scripts/Mission/MissionPanel.cs"))

    def test_accepts_cs_anywhere_via_wildcard(self) -> None:
        project = _make_project()
        _, norm_rel = ensure_readable(project, "Assets/Random/Other/Bar.cs")
        self.assertEqual(norm_rel, "Assets/Random/Other/Bar.cs")

    def test_rejects_disallowed_extension(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "Assets/Scripts/Foo.exe")

    def test_rejects_path_not_in_allow_globs(self) -> None:
        project = _make_project(allow_globs=("Assets/Scripts/**",))
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "Tools/SomeScript.cs")

    def test_rejects_denied_pattern(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "Library/PackageCache/foo.cs")

    def test_rejects_denied_extension_in_deny_globs(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_readable(project, "Assets/UI/icon.png")

    def test_top_level_markdown_via_star_md_rule(self) -> None:
        project = _make_project()
        _, norm_rel = ensure_readable(project, "README.md")
        self.assertEqual(norm_rel, "README.md")

    def test_ensure_searchable_accepts_subdirectory(self) -> None:
        project = _make_project()
        abs_path, norm_rel = ensure_searchable(project, "Assets/Scripts")
        self.assertEqual(norm_rel, "Assets/Scripts")
        self.assertTrue(str(abs_path).replace("\\", "/").endswith("Assets/Scripts"))

    def test_ensure_searchable_empty_returns_project_root(self) -> None:
        project = _make_project()
        abs_path, norm_rel = ensure_searchable(project, "")
        self.assertEqual(norm_rel, "")
        self.assertEqual(str(abs_path), str(project.root))

    def test_ensure_searchable_rejects_denied_dir(self) -> None:
        project = _make_project()
        with self.assertRaises(UnityPathAccessError):
            ensure_searchable(project, "Library/PackageCache")


if __name__ == "__main__":
    unittest.main()
