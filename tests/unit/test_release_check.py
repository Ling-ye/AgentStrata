from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    path = ROOT / "scripts" / "check_release.py"
    spec = importlib.util.spec_from_file_location("check_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_candidate(root: Path, *, version: str = "0.1.0") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "agentstrata"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        """# Changelog

## [Unreleased]

## [0.1.0] - 2026-07-30

### Added

- First public release.

[Unreleased]: https://github.com/Ling-ye/AgentStrata/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Ling-ye/AgentStrata/releases/tag/v0.1.0
""",
        encoding="utf-8",
    )


def _replace_changelog(root: Path, old: str, new: str) -> None:
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )


def _replace_last_changelog(root: Path, old: str, new: str) -> None:
    changelog = root / "CHANGELOG.md"
    before, separator, after = changelog.read_text(encoding="utf-8").rpartition(old)
    assert separator
    changelog.write_text(before + new + after, encoding="utf-8")


def test_validate_release_returns_matching_changelog_section(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)

    notes = checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))

    assert "First public release" in notes
    assert "[Unreleased]:" not in notes


def test_validate_release_accepts_past_release_date(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    _replace_changelog(tmp_path, "2026-07-30", "2026-07-29")

    notes = checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))

    assert "First public release" in notes


def test_validate_release_rejects_development_version(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path, version="0.1.0.dev0")

    with pytest.raises(checker.ReleaseCheckError, match="does not match"):
        checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))


def test_validate_release_rejects_non_ascii_semver_digits(tmp_path: Path) -> None:
    checker = _load_checker()

    with pytest.raises(checker.ReleaseCheckError, match="stable SemVer"):
        checker.validate_release(tmp_path, "v1\u0661.0.0")


def test_validate_release_requires_trailing_comparison_links(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    _replace_changelog(tmp_path, "[0.1.0]: https://", "[x]: https://")

    with pytest.raises(checker.ReleaseCheckError, match="trailing reference"):
        checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))


def test_validate_release_rejects_duplicate_release_heading(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    _replace_changelog(
        tmp_path,
        "### Added",
        "## [0.1.0] - 2026-07-30\n\n### Added",
    )

    with pytest.raises(checker.ReleaseCheckError, match="exactly one release heading"):
        checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))


def test_validate_release_requires_target_immediately_after_unreleased(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    _replace_changelog(
        tmp_path,
        "## [0.1.0] - 2026-07-30",
        "## [0.0.9] - 2026-07-29\n\n- Earlier release.\n\n"
        "## [0.1.0] - 2026-07-30",
    )

    with pytest.raises(checker.ReleaseCheckError, match="first level-two heading"):
        checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))


def test_validate_release_rejects_link_only_inside_notes(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    expected_link = "[0.1.0]: https://github.com/Ling-ye/AgentStrata/releases/tag/v0.1.0"
    _replace_changelog(
        tmp_path,
        "- First public release.",
        f"- First public release.\n\n{expected_link}\n\n- Still release notes.",
    )
    _replace_last_changelog(tmp_path, expected_link + "\n", "[x]: https://example.invalid\n")

    with pytest.raises(checker.ReleaseCheckError, match="trailing reference"):
        checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))


def test_validate_release_does_not_truncate_reference_inside_notes(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    _replace_changelog(
        tmp_path,
        "- First public release.",
        "- First public release.\n\n"
        "[design]: https://example.invalid/design\n\n"
        "- Notes continue after the reference.",
    )

    notes = checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))

    assert "Notes continue after the reference" in notes


def test_validate_release_rejects_duplicate_trailing_link(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    release_link = "[0.1.0]: https://github.com/Ling-ye/AgentStrata/releases/tag/v0.1.0"
    _replace_changelog(tmp_path, release_link, f"{release_link}\n{release_link}")

    with pytest.raises(checker.ReleaseCheckError, match="trailing reference"):
        checker.validate_release(tmp_path, "v0.1.0", today=date(2026, 7, 30))


def test_validate_release_defaults_to_current_utc_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    monkeypatch.setattr(checker, "_utc_today", lambda: date(2026, 7, 29))

    with pytest.raises(checker.ReleaseCheckError, match="UTC reference date"):
        checker.validate_release(tmp_path, "v0.1.0")


def test_main_accepts_explicit_utc_reference_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_checker()
    _write_candidate(tmp_path)
    notes_output = tmp_path / "release-notes.md"
    monkeypatch.setattr(checker, "ROOT", tmp_path)

    result = checker.main(
        [
            "--tag",
            "v0.1.0",
            "--as-of-date-utc",
            "2026-07-30",
            "--notes-output",
            str(notes_output),
        ]
    )

    assert result == 0
    assert "First public release" in notes_output.read_text(encoding="utf-8")


def test_release_build_lock_is_exact_and_matches_pyproject() -> None:
    expected_versions = {
        "build": "1.5.0",
        "packaging": "26.2",
        "pyproject-hooks": "1.2.0",
        "setuptools": "83.0.0",
        "tomli": "2.4.1",
        "wheel": "0.47.0",
    }
    lock_lines = (ROOT / "requirements" / "release-build.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    content_lines = [
        line for line in lock_lines if line and not line.startswith("#")
    ]
    assert len(content_lines) == len(expected_versions) * 2

    entries: dict[str, tuple[str, str]] = {}
    for offset in range(0, len(content_lines), 2):
        requirement_line = content_lines[offset]
        hash_line = content_lines[offset + 1]
        assert requirement_line.endswith(" \\")
        requirement = requirement_line[:-2]
        requirement_match = re.fullmatch(
            r"(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)==(?P<version>[^\s]+)",
            requirement,
        )
        assert requirement_match is not None
        name = requirement_match.group("name")
        assert name == re.sub(r"[-_.]+", "-", name).lower()
        assert name not in entries

        hash_match = re.fullmatch(
            r"    --hash=sha256:(?P<digest>[0-9a-f]{64})",
            hash_line,
        )
        assert hash_match is not None
        entries[name] = (
            requirement_match.group("version"),
            hash_match.group("digest"),
        )

    assert {name: version for name, (version, _) in entries.items()} == (
        expected_versions
    )

    pyproject = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    def selected_exact_requirements(
        requirements: list[str],
        selected_names: set[str],
    ) -> dict[str, str]:
        selected: dict[str, str] = {}
        for requirement_text in requirements:
            raw_name = re.split(r"[\s<>=!~\[]", requirement_text, maxsplit=1)[0]
            canonical_name = re.sub(r"[-_.]+", "-", raw_name).lower()
            if canonical_name in selected_names:
                assert canonical_name not in selected
                selected[canonical_name] = requirement_text
        return selected

    dev_names = {"build", "setuptools", "wheel"}
    expected_dev = {
        name: f"{name}=={expected_versions[name]}" for name in dev_names
    }
    assert selected_exact_requirements(
        pyproject["project"]["optional-dependencies"]["dev"],
        dev_names,
    ) == expected_dev

    build_system_names = {"setuptools", "wheel"}
    expected_build_system = {
        name: f"{name}=={expected_versions[name]}" for name in build_system_names
    }
    assert selected_exact_requirements(
        pyproject["build-system"]["requires"],
        build_system_names,
    ) == expected_build_system

    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    for argument in (
        "--isolated",
        "--index-url https://pypi.org/simple",
        "--no-deps",
        "--require-hashes",
        "--only-binary=:all:",
        "--requirement requirements/release-build.txt",
    ):
        assert workflow.count(argument) == 2
