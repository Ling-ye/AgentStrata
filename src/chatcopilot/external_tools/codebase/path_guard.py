"""Repository-relative path validation shared by codebase tools."""
from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Iterable

from chatcopilot.external_tools.codebase.config import CodeRepositoryConfig


class CodebasePathAccessError(PermissionError):
    pass


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    for raw_pattern in patterns:
        pattern = raw_pattern.replace("\\", "/").strip()
        if not pattern:
            continue
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(normalized, pattern[3:]):
            return True
    return False


def ensure_searchable(
    repository: CodeRepositoryConfig,
    rel_path: str = "",
) -> tuple[Path, str]:
    if not str(rel_path or "").strip():
        target = repository.root
        normalized = ""
    else:
        target, normalized = _resolve_relative(repository, rel_path)
    if normalized and matches_any(normalized, repository.deny_globs):
        raise CodebasePathAccessError(
            f"path is denied in repository {repository.repository_id!r}: {normalized}"
        )
    _ensure_inside_root(repository, target, normalized)
    return target, normalized


def ensure_readable(
    repository: CodeRepositoryConfig,
    rel_path: str,
) -> tuple[Path, str]:
    target, normalized = _resolve_relative(repository, rel_path)
    if not normalized:
        raise CodebasePathAccessError("rel_path resolves to the repository root")
    if matches_any(normalized, repository.deny_globs):
        raise CodebasePathAccessError(
            f"path is denied in repository {repository.repository_id!r}: {normalized}"
        )
    if repository.include_globs and not matches_any(normalized, repository.include_globs):
        raise CodebasePathAccessError(
            f"path is outside include_globs for repository {repository.repository_id!r}: {normalized}"
        )
    suffix = target.suffix.lower()
    if repository.allow_extensions and suffix not in repository.allow_extensions:
        raise CodebasePathAccessError(
            f"extension {suffix or '<none>'!r} is not readable: {normalized}"
        )
    _ensure_inside_root(repository, target, normalized)
    return target, normalized


def _resolve_relative(
    repository: CodeRepositoryConfig,
    rel_path: str,
) -> tuple[Path, str]:
    raw = str(rel_path or "").strip().replace("\\", "/")
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or raw.startswith(("/", "\\")):
        raise CodebasePathAccessError("codebase paths must be non-empty and relative")
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise CodebasePathAccessError(f"path escapes repository root: {rel_path}")
            parts.pop()
            continue
        parts.append(part)
    normalized = PurePosixPath(*parts).as_posix() if parts else ""
    return repository.root.joinpath(*parts), normalized


def _ensure_inside_root(
    repository: CodeRepositoryConfig,
    target: Path,
    normalized: str,
) -> None:
    try:
        target.resolve(strict=False).relative_to(repository.root.resolve(strict=False))
    except ValueError as exc:
        raise CodebasePathAccessError(
            f"resolved path escapes repository root: {normalized}"
        ) from exc


__all__ = [
    "CodebasePathAccessError",
    "ensure_readable",
    "ensure_searchable",
    "matches_any",
]

