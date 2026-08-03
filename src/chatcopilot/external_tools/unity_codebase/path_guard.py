"""Path access guard for ``unity_codebase`` tools.

All Tier 2 tools accept a project id + a relative path (or pattern). This
module validates:

1. The project exists in the registry.
2. The relative path stays under the project ``root`` (no ``..`` escape).
3. The resolved relative path matches at least one ``allow_globs`` entry.
4. The resolved relative path does not match any ``deny_globs`` entry.
5. The extension lies in ``allow_extensions`` (for read operations).

Glob matching uses :mod:`fnmatch` with normalized forward-slash paths and
``**`` recursive matching. Patterns are matched both against the full
relative path and against trailing path segments to mirror Cursor's
``.cursorindexingignore`` semantics.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import Iterable

from chatcopilot.external_tools.unity_codebase.config import UnityProjectConfig


class UnityPathAccessError(PermissionError):
    """Raised when a path is rejected by the project allow-list."""


def _normalize_rel(rel: str) -> str:
    norm = rel.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    while norm.endswith("/") and len(norm) > 1:
        norm = norm[:-1]
    return norm


def _matches_glob(target: str, pattern: str) -> bool:
    pat = pattern.replace("\\", "/")
    if fnmatch.fnmatchcase(target, pat):
        return True
    if "**/" in pat:
        tail = pat.split("**/", 1)[1]
        if fnmatch.fnmatchcase(target, tail):
            return True
        for i in range(len(target.split("/"))):
            suffix = "/".join(target.split("/")[i:])
            if fnmatch.fnmatchcase(suffix, tail):
                return True
    return False


def _matches_any(target: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if not pattern.strip():
            continue
        if _matches_glob(target, pattern):
            return True
    return False


def _resolve_relative(project: UnityProjectConfig, rel_path: str) -> tuple[Path, str]:
    """Resolve a user-supplied relative path against the project root.

    Returns ``(absolute_path, normalized_rel)``. Raises
    ``UnityPathAccessError`` if the input is empty, absolute, or escapes the
    project root via ``..``.
    """
    if not rel_path or not str(rel_path).strip():
        raise UnityPathAccessError("rel_path is empty")

    candidate = Path(str(rel_path).strip())
    if candidate.is_absolute() or str(candidate).startswith(("/", "\\")):
        raise UnityPathAccessError(
            f"rel_path must be relative to project root, got absolute: {rel_path}"
        )

    parts: list[str] = []
    for part in candidate.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise UnityPathAccessError(f"rel_path escapes project root: {rel_path}")
            parts.pop()
            continue
        parts.append(part)

    norm_rel = str(PurePosixPath(*parts)) if parts else ""
    abs_path = project.root.joinpath(*parts) if parts else project.root
    return abs_path, norm_rel


def ensure_searchable(project: UnityProjectConfig, rel_path: str = "") -> tuple[Path, str]:
    """Validate that ``rel_path`` (possibly empty == project root) is a valid search root."""
    if not rel_path or not str(rel_path).strip():
        return project.root, ""
    abs_path, norm_rel = _resolve_relative(project, rel_path)
    if norm_rel and _matches_any(norm_rel, project.deny_globs):
        raise UnityPathAccessError(
            f"search root matches deny_globs for project {project.project_id!r}: {norm_rel}"
        )
    return abs_path, norm_rel


def ensure_readable(project: UnityProjectConfig, rel_path: str) -> tuple[Path, str]:
    """Validate that ``rel_path`` is a readable file under the project allow-list."""
    abs_path, norm_rel = _resolve_relative(project, rel_path)
    if not norm_rel:
        raise UnityPathAccessError("rel_path resolves to project root, not a file")

    if _matches_any(norm_rel, project.deny_globs):
        raise UnityPathAccessError(
            f"path matches deny_globs for project {project.project_id!r}: {norm_rel}"
        )

    allow_ok = (not project.allow_globs) or _matches_any(norm_rel, project.allow_globs)
    if not allow_ok:
        raise UnityPathAccessError(
            f"path is not covered by allow_globs for project {project.project_id!r}: {norm_rel}"
        )

    if project.allow_extensions:
        ext = os.path.splitext(norm_rel)[1].lower()
        if ext and ext not in project.allow_extensions:
            raise UnityPathAccessError(
                f"extension {ext!r} not allowed for project {project.project_id!r}: {norm_rel}"
            )
    return abs_path, norm_rel


__all__ = [
    "UnityPathAccessError",
    "ensure_readable",
    "ensure_searchable",
]
