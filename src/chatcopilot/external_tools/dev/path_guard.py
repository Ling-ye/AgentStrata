"""Path safety enforcement for dev tools.

Validates that requested paths are within allowed boundaries and not in
denied patterns. Simpler than the old codebase path_guard since we operate
directly on the working directory without worktree isolation.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath

from chatcopilot.contracts.development import current_development_task_scope
from chatcopilot.external_tools.dev.config import DevConfig


class DevPathAccessError(PermissionError):
    """Raised when a path violates safety rules."""
    pass


def resolve_path(config: DevConfig, raw_path: str) -> tuple[Path, str]:
    """Resolve a user-provided path to an absolute Path and normalized relative string.

    Raises DevPathAccessError for invalid or escaping paths.
    """
    raw = str(raw_path or "").strip().replace("\\", "/")
    if not raw:
        raise DevPathAccessError("path must not be empty")

    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        try:
            rel = resolved.relative_to(config.repo_root)
        except ValueError:
            raise DevPathAccessError(
                f"absolute path is outside dev workspace: {raw}"
            )
        normalized = rel.as_posix()
    else:
        parts: list[str] = []
        for part in PurePosixPath(raw).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise DevPathAccessError(f"path escapes project root: {raw}")
                parts.pop()
                continue
            parts.append(part)
        if not parts:
            raise DevPathAccessError("path resolves to project root (use '.' explicitly for listing)")
        normalized = "/".join(parts)
        resolved = config.repo_root.joinpath(*parts)

    return resolved, normalized


def ensure_readable(config: DevConfig, raw_path: str) -> tuple[Path, str]:
    """Validate path is allowed for reading."""
    resolved, normalized = resolve_path(config, raw_path)
    _check_denied(config, normalized, raw_path)
    return resolved, normalized


def ensure_writable(config: DevConfig, raw_path: str) -> tuple[Path, str]:
    """Validate path is allowed for writing."""
    resolved, normalized = resolve_path(config, raw_path)
    _check_denied(config, normalized, raw_path)
    _check_allowed(config, normalized, raw_path)
    _check_task_scope(normalized, raw_path)
    return resolved, normalized


def ensure_listable(config: DevConfig, raw_path: str) -> tuple[Path, str]:
    """Validate path for directory listing. Empty/dot means repo root."""
    raw = str(raw_path or "").strip()
    if not raw or raw == ".":
        return config.repo_root, ""
    return ensure_readable(config, raw_path)


def _check_denied(config: DevConfig, normalized: str, original: str) -> None:
    if _matches_any(normalized, config.denied_paths):
        raise DevPathAccessError(f"path is denied: {original}")


def _check_allowed(config: DevConfig, normalized: str, original: str) -> None:
    if not _matches_any(normalized, config.allowed_paths):
        raise DevPathAccessError(f"path is outside allowed scope: {original}")


def _check_task_scope(normalized: str, original: str) -> None:
    scope = current_development_task_scope()
    if scope is None:
        return
    if not _matches_any(normalized, scope.allowed_paths):
        label = f" for delegated task {scope.task_label}" if scope.task_label else ""
        raise DevPathAccessError(
            f"path is outside delegated write_scope{label}: {original}"
        )


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").strip()
        if not pattern:
            continue
        if fnmatch.fnmatchcase(path, pattern):
            return True
        prefix = pattern.rstrip("/")
        if prefix and not any(char in prefix for char in "*?["):
            if path.startswith(prefix + "/"):
                return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
            return True
    return False


__all__ = [
    "DevPathAccessError",
    "ensure_listable",
    "ensure_readable",
    "ensure_writable",
    "resolve_path",
]
