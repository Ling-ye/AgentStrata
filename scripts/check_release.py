#!/usr/bin/env python3
"""Validate release metadata and extract deterministic GitHub release notes."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STABLE_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
REFERENCE_RE = re.compile(r"^\[[^\]\n]+\]:[ \t]+https?://\S+[ \t]*$")


class ReleaseCheckError(ValueError):
    """Raised when release metadata is incomplete or inconsistent."""


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_utc_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "UTC reference date must use YYYY-MM-DD"
        ) from exc


def _project_version(pyproject_text: str) -> str:
    in_project = False
    for raw_line in pyproject_text.splitlines():
        line = raw_line.strip()
        if line == "[project]":
            in_project = True
            continue
        if in_project and line.startswith("["):
            break
        if in_project:
            match = re.fullmatch(r'version\s*=\s*"([^"]+)"', line)
            if match:
                return match.group(1)
    raise ReleaseCheckError("pyproject.toml has no literal [project].version")


def _trailing_references(lines: list[str]) -> tuple[int, list[str]]:
    cursor = len(lines)
    while cursor and not lines[cursor - 1].strip():
        cursor -= 1

    references: list[str] = []
    while cursor:
        line = lines[cursor - 1]
        if REFERENCE_RE.fullmatch(line):
            references.append(line.strip())
            cursor -= 1
            continue
        if references and not line.strip():
            cursor -= 1
            continue
        break
    references.reverse()
    return cursor, references


def _unique_release_heading(lines: list[str], version: str) -> tuple[int, date]:
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\] - "
        r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*$"
    )
    matches = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := pattern.fullmatch(line)) is not None
    ]
    if not matches:
        raise ReleaseCheckError(f"CHANGELOG.md needs '## [{version}] - YYYY-MM-DD'")
    if len(matches) != 1:
        raise ReleaseCheckError(
            f"CHANGELOG.md must contain exactly one release heading for {version}"
        )
    index, raw_date = matches[0]
    return index, date.fromisoformat(raw_date)


def _require_release_heading_order(
    lines: list[str],
    *,
    unreleased_index: int,
    release_index: int,
    version: str,
) -> None:
    next_heading_index = next(
        (
            index
            for index, line in enumerate(lines[unreleased_index + 1 :], unreleased_index + 1)
            if line.startswith("## ")
        ),
        None,
    )
    if next_heading_index != release_index:
        raise ReleaseCheckError(
            f"CHANGELOG.md release {version} must be the first level-two heading "
            "after Unreleased"
        )


def validate_release(root: Path, tag: str, *, today: date | None = None) -> str:
    match = STABLE_TAG_RE.fullmatch(tag)
    if not match:
        raise ReleaseCheckError("release tag must be a stable SemVer tag such as v0.1.0")
    version = tag.removeprefix("v")
    pyproject_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project_version = _project_version(pyproject_text)
    if project_version != version:
        raise ReleaseCheckError(f"tag {tag} does not match [project].version {project_version!r}")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = changelog.splitlines()
    unreleased_indices = [
        index for index, line in enumerate(lines) if line.strip() == "## [Unreleased]"
    ]
    if len(unreleased_indices) != 1:
        raise ReleaseCheckError("CHANGELOG.md must contain exactly one Unreleased heading")

    heading_index, release_date = _unique_release_heading(lines, version)
    _require_release_heading_order(
        lines,
        unreleased_index=unreleased_indices[0],
        release_index=heading_index,
        version=version,
    )
    if release_date > (today if today is not None else _utc_today()):
        raise ReleaseCheckError("CHANGELOG.md release date cannot be after the UTC reference date")

    next_heading = next(
        (index for index in range(heading_index + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    reference_start, references = _trailing_references(lines)
    section_end = min(next_heading, reference_start)
    notes = "\n".join(lines[heading_index + 1 : section_end]).strip()
    if not notes:
        raise ReleaseCheckError("CHANGELOG.md release section is empty")

    expected_unreleased = (
        f"[Unreleased]: https://github.com/Ling-ye/AgentStrata/compare/{tag}...HEAD"
    )
    expected_release = f"[{version}]: https://github.com/Ling-ye/AgentStrata/releases/tag/{tag}"
    invalid_counts = [
        link for link in (expected_unreleased, expected_release) if references.count(link) != 1
    ]
    if invalid_counts:
        raise ReleaseCheckError(
            "CHANGELOG.md needs exactly one trailing reference for: " + ", ".join(invalid_counts)
        )
    return notes + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="stable release tag, for example v0.1.0")
    parser.add_argument(
        "--notes-output",
        type=Path,
        help="write the matching CHANGELOG section to this file",
    )
    parser.add_argument(
        "--as-of-date-utc",
        type=_parse_utc_date,
        help="UTC reference date in YYYY-MM-DD; defaults to the current UTC date",
    )
    args = parser.parse_args(argv)
    try:
        notes = validate_release(ROOT, args.tag, today=args.as_of_date_utc)
    except (OSError, ReleaseCheckError, ValueError) as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1
    if args.notes_output:
        args.notes_output.write_text(notes, encoding="utf-8")
    print(f"release metadata is consistent for {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
