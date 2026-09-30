from dataclasses import replace
from pathlib import Path
import os

import pytest

from chatcopilot.external_tools.codebase.config import CodeRepositoryConfig
from chatcopilot.external_tools.codebase.fallback_search import list_visible_files, search_visible_files


def test_denied_subtrees_are_not_opened(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for name in ("src/app.py", "README.md", ".venv/lib/dependency.py",
                 "web/node_modules/package/index.js", "blocked/nested/hidden.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("needle\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    repository = CodeRepositoryConfig("demo", "Demo", root,
        deny_globs=(".venv/**", "**/node_modules/**", "blocked/**"))
    original = os.scandir
    opened = []

    def inspect_directory(path):
        directory = Path(path)
        assert directory.name not in {".venv", "node_modules", "blocked", "linked"}
        opened.append(directory)
        return original(path)

    monkeypatch.setattr(os, "scandir", inspect_directory)
    assert list_visible_files(repository) == ["README.md", "src/app.py"]
    assert root / "src" in opened
    assert search_visible_files(repository, query="needle", search_root=root,
                                fixed_strings=True) == ["README.md:1:needle", "src/app.py:1:needle"]


def test_file_denials_do_not_prune_visible_descendants(tmp_path):
    for name in ("src/app.py", "src/private.py", "notes/visible.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n")
    repository = CodeRepositoryConfig("demo", "Demo", tmp_path,
        deny_globs=("src/private.py", "notes"))
    assert list_visible_files(repository) == ["notes/visible.py", "src/app.py"]
    scoped = replace(repository, include_globs=("src/**",))
    assert list_visible_files(scoped, tmp_path / "src") == ["src/app.py"]
    with pytest.raises(ValueError, match="escapes repository"):
        list_visible_files(scoped, tmp_path.parent)
