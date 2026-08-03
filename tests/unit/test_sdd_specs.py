from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_FILES = ("spec.yaml", "acceptance.md", "verification.md")


def _load_checker():
    script = ROOT / "scripts" / "check_sdd_specs.py"
    spec = importlib.util.spec_from_file_location("check_sdd_specs", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _single_file_spec(*, extra_frontmatter: str = "", verification: str = "not run") -> str:
    return f"""---
id: demo
type: feature
status: implemented
created: 2026-07-17
{extra_frontmatter}---

## Summary

Demo summary.

## Design

Demo design.

## Acceptance

Demo acceptance.

## Verification

{verification}
"""


def _make_root(tmp_path: Path, spec_text: str) -> Path:
    spec_dir = tmp_path / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    template = tmp_path / "specs" / "_template"
    template.mkdir()
    (template / "spec.md").write_text("# template\n", encoding="utf-8")
    (spec_dir / "spec.md").write_text(spec_text, encoding="utf-8")
    return spec_dir


def test_specs_have_required_files_and_schema() -> None:
    checker = _load_checker()
    assert checker.check_specs(ROOT) == []


def test_template_is_single_file() -> None:
    template = ROOT / "specs" / "_template"
    assert (template / "spec.md").is_file()
    assert not any((template / name).exists() for name in LEGACY_FILES)


def test_implemented_spec_does_not_require_pass_wording(tmp_path: Path) -> None:
    checker = _load_checker()
    _make_root(tmp_path, _single_file_spec())
    assert checker.check_specs(tmp_path) == []


def test_frontmatter_rejects_extra_keys_and_legacy_files(tmp_path: Path) -> None:
    checker = _load_checker()
    spec_dir = _make_root(
        tmp_path,
        _single_file_spec(extra_frontmatter="allowed_paths: [src/**]\n"),
    )
    (spec_dir / "verification.md").write_text("legacy\n", encoding="utf-8")

    errors = checker.check_specs(tmp_path)

    assert any("unsupported frontmatter keys ['allowed_paths']" in error for error in errors)
    assert any("legacy file must be removed: verification.md" in error for error in errors)


def test_required_sections_must_be_nonempty_and_ordered(tmp_path: Path) -> None:
    checker = _load_checker()
    text = _single_file_spec().replace("## Design", "## Verification", 1)
    _make_root(tmp_path, text)

    errors = checker.check_specs(tmp_path)

    assert any("required sections must appear exactly once and in order" in error for error in errors)
