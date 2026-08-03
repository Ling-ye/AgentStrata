"""Portable read-only search used when ripgrep is unavailable."""
from __future__ import annotations

import re
from pathlib import Path

from chatcopilot.external_tools.codebase.config import CodeRepositoryConfig
from chatcopilot.external_tools.codebase.path_guard import matches_any


def is_visible(repository: CodeRepositoryConfig, rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").removeprefix("./")
    if matches_any(normalized, repository.deny_globs):
        return False
    if repository.include_globs and not matches_any(normalized, repository.include_globs):
        return False
    return not repository.allow_extensions or Path(normalized).suffix.lower() in repository.allow_extensions


def list_visible_files(
    repository: CodeRepositoryConfig,
    search_root: Path | None = None,
) -> list[str]:
    root = (search_root or repository.root).resolve()
    repository_root = repository.root.resolve()
    try:
        root.relative_to(repository_root)
    except ValueError as exc:
        raise ValueError(f"search root escapes repository: {root}") from exc

    files: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            rel_path = path.resolve().relative_to(repository_root).as_posix()
        except ValueError:
            continue
        if is_visible(repository, rel_path):
            files.append(rel_path)
    return sorted(files)


def search_visible_files(
    repository: CodeRepositoryConfig,
    *,
    query: str,
    search_root: Path,
    fixed_strings: bool,
    file_glob: str = "",
    max_count: int = 100,
) -> list[str]:
    matcher = None if fixed_strings else re.compile(query)
    hits: list[str] = []
    for rel_path in list_visible_files(repository, search_root):
        if file_glob and not matches_any(rel_path, (file_glob,)):
            continue
        path = repository.root / rel_path
        if path.stat().st_size > repository.max_read_bytes:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in content:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            matched = query in line if fixed_strings else bool(matcher and matcher.search(line))
            if matched:
                hits.append(f"{rel_path}:{line_no}:{line}")
                if len(hits) >= max_count:
                    return hits
    return hits


__all__ = ["is_visible", "list_visible_files", "search_visible_files"]
