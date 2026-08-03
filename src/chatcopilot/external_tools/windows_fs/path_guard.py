"""Path access guard for the ``windows_fs`` capability.

Validates that any absolute path requested by ``win_*`` tools:

1. Stays under one of ``WindowsFsConfig.allowed_roots`` (after normalization).
2. Does not match any deny glob in ``denied_patterns``.
3. Has an extension in ``allowed_extensions`` (when extensions are configured).

Cross-platform notes:

- We compare paths case-insensitively on Windows (mirrors NTFS semantics).
- We unify separators to ``/`` so the same allow-list works in WSL2 (where
  paths look like ``/mnt/f/...``) and native Windows (``F:/...``).
- We deliberately do not rely on ``Path.is_absolute()`` because on Windows
  POSIX-style paths like ``/mnt/f/...`` are not recognized as absolute; we
  perform a string-based check instead so the same code path validates both
  WSL and Windows inputs.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterable

from chatcopilot.external_tools.windows_fs.config import WindowsFsConfig


_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")


def _is_absolute_path(value: str) -> bool:
    return bool(_ABS_PATH_RE.match(value))


class PathAccessError(PermissionError):
    """Raised when a path is rejected by the windows_fs allow-list."""


def _normalize(value: str) -> str:
    """Unify separators and (on Windows) case so prefix comparisons work."""
    norm = value.replace("\\", "/")
    while norm.endswith("/") and len(norm) > 1:
        norm = norm[:-1]
    if os.name == "nt":
        norm = norm.lower()
    return norm


def _is_within_any_root(target_norm: str, roots: Iterable[str]) -> bool:
    for root in roots:
        root_norm = _normalize(root)
        if not root_norm:
            continue
        if target_norm == root_norm:
            return True
        if target_norm.startswith(root_norm + "/"):
            return True
    return False


def _matches_any_glob(target_norm: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        pat_norm = _normalize(pattern)
        if not pat_norm:
            continue
        if fnmatch.fnmatchcase(target_norm, pat_norm):
            return True
        if "**/" in pat_norm:
            tail = pat_norm.split("**/", 1)[1]
            if fnmatch.fnmatchcase(target_norm, "*/" + tail):
                return True
            if fnmatch.fnmatchcase(target_norm, tail):
                return True
    return False


def normalize_input_path(raw: str) -> Path:
    """Resolve a user-supplied absolute path string into a ``Path`` without I/O.

    We deliberately avoid ``Path.resolve(strict=True)`` so the guard can be run
    on paths that exist on a remote filesystem (e.g. ``/mnt/f/...`` from inside
    WSL where the file might not be accessible at validation time during
    tests). The caller's actual I/O will surface any missing-file errors.
    """
    if not raw or not str(raw).strip():
        raise PathAccessError("path is empty")
    text = os.path.expanduser(str(raw).strip())
    if not _is_absolute_path(text):
        raise PathAccessError(f"path must be absolute: {raw}")

    unified = text.replace("\\", "/")
    head: str
    body: str
    drive_match = re.match(r"^([A-Za-z]:)/", unified)
    if drive_match:
        head = drive_match.group(0)
        body = unified[len(head) :]
    elif unified.startswith("/"):
        head = "/"
        body = unified[1:]
    else:
        head = ""
        body = unified

    parts: list[str] = []
    for segment in body.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise PathAccessError(f"path escapes root via '..': {raw}")
            parts.pop()
            continue
        parts.append(segment)

    rebuilt = head + "/".join(parts)
    return Path(rebuilt)


def ensure_readable(path: str, cfg: WindowsFsConfig) -> Path:
    """Validate that ``path`` is allowed for read/search operations.

    Returns the normalized ``Path`` instance on success; raises
    ``PathAccessError`` on rejection.
    """
    resolved = normalize_input_path(path)
    target_norm = _normalize(str(resolved))

    if not cfg.allowed_roots:
        raise PathAccessError(
            "no allowed_roots configured; set CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS "
            "or CHATCOPILOT_WINDOWS_FS_ALLOWLIST"
        )
    if not _is_within_any_root(target_norm, cfg.allowed_roots):
        raise PathAccessError(
            f"path not under any allowed_roots: {resolved} "
            f"(allowed roots: {', '.join(cfg.allowed_roots) or '<empty>'})"
        )
    if _matches_any_glob(target_norm, cfg.denied_patterns):
        raise PathAccessError(f"path matches denied_patterns: {resolved}")

    if cfg.allowed_extensions:
        ext = resolved.suffix.lower()
        if ext and ext not in cfg.allowed_extensions and not resolved.is_dir():
            raise PathAccessError(
                f"extension {ext!r} not in allowed_extensions: {resolved}"
            )
    return resolved


def ensure_directory_searchable(path: str, cfg: WindowsFsConfig) -> Path:
    """Validate that ``path`` can be used as a search root.

    Same checks as ``ensure_readable`` minus the extension constraint (a
    directory has no meaningful extension).
    """
    resolved = normalize_input_path(path)
    target_norm = _normalize(str(resolved))

    if not cfg.allowed_roots:
        raise PathAccessError(
            "no allowed_roots configured; set CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS "
            "or CHATCOPILOT_WINDOWS_FS_ALLOWLIST"
        )
    if not _is_within_any_root(target_norm, cfg.allowed_roots):
        raise PathAccessError(
            f"path not under any allowed_roots: {resolved}"
        )
    if _matches_any_glob(target_norm, cfg.denied_patterns):
        raise PathAccessError(f"path matches denied_patterns: {resolved}")
    return resolved


__all__ = [
    "PathAccessError",
    "ensure_directory_searchable",
    "ensure_readable",
    "normalize_input_path",
]
