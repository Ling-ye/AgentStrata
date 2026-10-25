#!/usr/bin/env python3
"""Select optional CI checks; the core repository checks always run."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath

from check_repo import ROOT, _documentation_changes


EXTENSIONS = ("vtuber", "generator", "desktop")
SHARED_TESTS = {
    "tests/conftest.py", "tests/unit/conftest.py", "tests/model_settings_fixture.py",
    "tests/unit/test_plugins.py", "tests/unit/test_plugin_artifacts.py",
    "tests/unit/test_console_plugin_agents.py", "tests/unit/test_repository_validation.py",
}
ROOT_DOCUMENTS = {
    "README.md", "AGENTS.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    "SECURITY.md", "SUPPORT.md", "CHANGELOG.md",
}


def select(paths: tuple[str, ...] | None, *, event: str, reason: str = "unknown_base") -> dict:
    package = event == "workflow_dispatch"

    def all_extensions(why: str) -> dict:
        return dict.fromkeys(EXTENSIONS, True) | {"package": package, "reason": why}

    if package:
        return all_extensions("manual_full")
    if paths is None:
        return all_extensions(reason)
    selected = dict.fromkeys(EXTENSIONS, False)
    for path in paths:
        name = PurePosixPath(path).name
        if (path.startswith(("src/", ".github/", "scripts/", "requirements/", "tests/fixtures/"))
                or path in {"pyproject.toml", "uv.lock"} or path in SHARED_TESTS
                or (path.startswith("tests/") and name.startswith("test_ci_"))):
            return all_extensions("shared_changes")
        if path.startswith("integrations/open_llm_vtuber/") or (path.startswith("tests/") and name.startswith("test_vtuber")):
            selected.update(vtuber=True, desktop=True)
        elif path.startswith("integrations/live2d_generator/") or (path.startswith("tests/") and name.startswith(("test_live2d", "test_character_presets"))):
            selected.update(generator=True, desktop=True)
        elif path.startswith("integrations/live2d_desktop/"):
            selected["desktop"] = True
        elif path.startswith(("docs/", "specs/", "console/", "tests/")) or path in ROOT_DOCUMENTS:
            continue
        else:
            return all_extensions("unclassified_path")
    return selected | {"package": False, "reason": "related_changes" if any(selected.values()) else
                       "core_only" if paths else "no_changes"}


def repository_scope(root: Path, *, event: str, base: str | None) -> dict:
    if event == "workflow_dispatch":
        return select(None, event=event)
    if event not in {"push", "pull_request"}:
        return select(None, event=event, reason="unknown_event")
    if not base or (len(base) in {40, 64} and set(base) == {"0"}):
        return select(None, event=event)
    try:
        paths = _documentation_changes(root, base_ref=base)
    except ValueError:
        paths = None
    return select(paths, event=event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--base")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    result = repository_scope(args.root.resolve(), event=args.event, base=args.base)
    print(json.dumps(result, ensure_ascii=False))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write("## Extension check selection\n\n" +
                             "\n".join(f"- {key}: `{str(value).lower()}`" for key, value in result.items()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
