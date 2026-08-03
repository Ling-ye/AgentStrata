#!/usr/bin/env python3
"""Validate the lightweight single-file AgentStrata specs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = "spec.md"
LEGACY_FILES = ("spec.yaml", "acceptance.md", "verification.md")
REQUIRED_KEYS = {"id", "type", "status", "created"}
REQUIRED_SECTIONS = ("Summary", "Design", "Acceptance", "Verification")
VALID_TYPES = {
    "architecture",
    "data-migration",
    "deployment",
    "feature",
    "process",
    "public-contract",
    "refactor",
    "workflow",
}
VALID_STATUSES = {"draft", "accepted", "implemented", "superseded", "rejected"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _spec_dirs(root: Path = ROOT) -> list[Path]:
    specs = root / "specs"
    return sorted(path for path in specs.iterdir() if path.is_dir() and not path.name.startswith("_"))


def _frontmatter(text: str, *, source: str) -> tuple[dict[str, Any], str, list[str]]:
    if not text.startswith("---\n"):
        return {}, text, [f"{source}: missing YAML frontmatter"]
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text, [f"{source}: unterminated YAML frontmatter"]
    try:
        data = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError as exc:
        return {}, text, [f"{source}: invalid YAML frontmatter: {exc}"]
    if not isinstance(data, dict):
        return {}, text, [f"{source}: YAML frontmatter must be a mapping"]
    return data, text[marker + 5 :], []


def _section_bodies(body: str) -> tuple[dict[str, str], list[str]]:
    matches = list(re.finditer(r"(?m)^## (Summary|Design|Acceptance|Verification)\s*$", body))
    names = [match.group(1) for match in matches]
    errors: list[str] = []
    if names != list(REQUIRED_SECTIONS):
        errors.append(
            "required sections must appear exactly once and in order: "
            + ", ".join(REQUIRED_SECTIONS)
        )
        return {}, errors
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1)] = body[match.end() : end].strip()
    return sections, errors


def _validate_spec_dir(spec_dir: Path, root: Path) -> list[str]:
    errors: list[str] = []
    rel = spec_dir.relative_to(root)
    path = spec_dir / SPEC_FILE
    if not path.is_file():
        return [f"{rel}: missing {SPEC_FILE}"]
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return [f"{rel}/{SPEC_FILE}: file is empty"]

    source = f"{rel}/{SPEC_FILE}"
    data, body, frontmatter_errors = _frontmatter(text, source=source)
    errors.extend(frontmatter_errors)
    if not frontmatter_errors:
        keys = set(data)
        if keys != REQUIRED_KEYS:
            missing = sorted(REQUIRED_KEYS - keys)
            extra = sorted(keys - REQUIRED_KEYS)
            if missing:
                errors.append(f"{source}: missing frontmatter keys {missing}")
            if extra:
                errors.append(f"{source}: unsupported frontmatter keys {extra}")
        if data.get("id") != spec_dir.name:
            errors.append(f"{source}: id must match directory name")
        if data.get("type") not in VALID_TYPES:
            errors.append(f"{source}: invalid type {data.get('type')!r}")
        if data.get("status") not in VALID_STATUSES:
            errors.append(f"{source}: invalid status {data.get('status')!r}")
        if not DATE_RE.match(str(data.get("created") or "")):
            errors.append(f"{source}: created must use YYYY-MM-DD")

    sections, section_errors = _section_bodies(body)
    errors.extend(f"{source}: {error}" for error in section_errors)
    for name, section_body in sections.items():
        if not section_body:
            errors.append(f"{source}: {name} section must not be empty")

    for name in LEGACY_FILES:
        if (spec_dir / name).exists():
            errors.append(f"{rel}: legacy file must be removed: {name}")
    return errors


def check_specs(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    spec_dirs = _spec_dirs(root)
    if not spec_dirs:
        errors.append("expected at least one concrete spec under specs/")
    for spec_dir in spec_dirs:
        errors.extend(_validate_spec_dir(spec_dir, root))
    template = root / "specs" / "_template"
    if not (template / SPEC_FILE).is_file():
        errors.append(f"specs/_template: missing {SPEC_FILE}")
    for name in LEGACY_FILES:
        if (template / name).exists():
            errors.append(f"specs/_template: legacy file must be removed: {name}")
    return errors


def main() -> int:
    errors = check_specs()
    if not errors:
        print("OK: SDD-lite specs")
        return 0
    for error in errors:
        print(error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
