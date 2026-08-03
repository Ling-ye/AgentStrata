from __future__ import annotations

import os
import unittest

from chatcopilot.external_tools.windows_fs.config import WindowsFsConfig
from chatcopilot.external_tools.windows_fs.path_guard import (
    PathAccessError,
    ensure_directory_searchable,
    ensure_readable,
)


def _make_config(**overrides) -> WindowsFsConfig:
    defaults = dict(
        allowed_roots=("/mnt/f/SampleGame/v1d3/proj", "F:/SampleGame/v1d3/proj"),
        denied_patterns=("**/Library/**", "**/.git/**"),
        allowed_extensions=(".cs", ".lua", ".md"),
        max_read_bytes=1_048_576,
    )
    defaults.update(overrides)
    return WindowsFsConfig(**defaults)


class WindowsFsPathGuardTests(unittest.TestCase):
    def test_empty_allowlist_fails_closed(self) -> None:
        cfg = _make_config(allowed_roots=())
        with self.assertRaisesRegex(PathAccessError, "no allowed_roots configured"):
            ensure_readable("/mnt/c/example/Assets/Foo.cs", cfg)
        with self.assertRaisesRegex(PathAccessError, "no allowed_roots configured"):
            ensure_directory_searchable("/mnt/c/example/Assets", cfg)

    def test_rejects_relative_path(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_readable("relative/path.cs", cfg)

    def test_rejects_empty_path(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_readable("", cfg)

    def test_rejects_dotdot_escape(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_readable("/mnt/f/SampleGame/v1d3/proj/../../etc/passwd", cfg)

    def test_rejects_path_outside_allowed_roots(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_readable("/mnt/c/Windows/notepad.exe", cfg)

    def test_accepts_path_inside_wsl_root(self) -> None:
        cfg = _make_config()
        result = ensure_readable("/mnt/f/SampleGame/v1d3/proj/Assets/Scripts/Foo.cs", cfg)
        self.assertEqual(
            str(result).replace("\\", "/"),
            "/mnt/f/SampleGame/v1d3/proj/Assets/Scripts/Foo.cs",
        )

    def test_accepts_path_inside_windows_root(self) -> None:
        cfg = _make_config()
        result = ensure_readable("F:/SampleGame/v1d3/proj/Assets/Bar.cs", cfg)
        # On Windows path stays as F:\... ; on Linux it stays F:/... .
        self.assertTrue(str(result).replace("\\", "/").endswith("SampleGame/v1d3/proj/Assets/Bar.cs"))

    def test_rejects_denied_pattern(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_readable(
                "/mnt/f/SampleGame/v1d3/proj/Library/PackageCache/foo.cs", cfg
            )

    def test_rejects_disallowed_extension(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_readable("/mnt/f/SampleGame/v1d3/proj/Assets/big.exe", cfg)

    def test_directory_searchable_accepts_any_extension(self) -> None:
        cfg = _make_config()
        result = ensure_directory_searchable("/mnt/f/SampleGame/v1d3/proj/Assets/Scripts", cfg)
        self.assertTrue(str(result).replace("\\", "/").endswith("Assets/Scripts"))

    def test_directory_searchable_rejects_denied_subdir(self) -> None:
        cfg = _make_config()
        with self.assertRaises(PathAccessError):
            ensure_directory_searchable("/mnt/f/SampleGame/v1d3/proj/Library/sub", cfg)

    @unittest.skipUnless(os.name == "nt", "case-insensitivity is Windows-specific")
    def test_windows_case_insensitive_root_match(self) -> None:
        cfg = _make_config()
        ensure_readable("f:/SampleGame/V1D3/PROJ/Assets/Foo.cs", cfg)


if __name__ == "__main__":
    unittest.main()
