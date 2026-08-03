"""Load and validate the ``windows_fs`` global allow-list."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from chatcopilot.external_tools.shared.env_template import expand_in_tree

_DEFAULT_ALLOWLIST_FILENAME = "allowlist.yaml"
_ENV_EXTRA_ROOTS = "CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS"
_ENV_ALLOWLIST_PATH = "CHATCOPILOT_WINDOWS_FS_ALLOWLIST"


@dataclass(frozen=True)
class WindowsFsConfig:
    """Resolved configuration for the ``windows_fs`` capability."""

    allowed_roots: tuple[str, ...]
    denied_patterns: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
    max_read_bytes: int = 1_048_576


@dataclass(frozen=True)
class _RawConfig:
    """Internal representation before env merge."""

    allowed_roots: List[str] = field(default_factory=list)
    denied_patterns: List[str] = field(default_factory=list)
    allowed_extensions: List[str] = field(default_factory=list)
    max_read_bytes: int = 1_048_576


_cached_config: Optional[WindowsFsConfig] = None
_cached_path: Optional[Path] = None


def _allowlist_path() -> Path:
    override = os.environ.get(_ENV_ALLOWLIST_PATH)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / _DEFAULT_ALLOWLIST_FILENAME


def _normalize_extension(ext: str) -> str:
    ext = ext.strip().lower()
    if not ext:
        return ext
    return ext if ext.startswith(".") else f".{ext}"


def _parse_raw(data: dict) -> _RawConfig:
    return _RawConfig(
        allowed_roots=[str(item) for item in (data.get("allowed_roots") or []) if str(item).strip()],
        denied_patterns=[str(item) for item in (data.get("denied_patterns") or []) if str(item).strip()],
        allowed_extensions=[_normalize_extension(str(item)) for item in (data.get("allowed_extensions") or [])],
        max_read_bytes=int(data.get("max_read_bytes") or 1_048_576),
    )


def _merge_env_extra_roots(roots: List[str]) -> List[str]:
    extra = os.environ.get(_ENV_EXTRA_ROOTS, "")
    if not extra.strip():
        return roots
    parts = [p.strip() for p in extra.split(",") if p.strip()]
    return roots + parts


def load_config(*, force_reload: bool = False) -> WindowsFsConfig:
    """Load the allow-list from YAML, expand env templates, then cache the result."""

    global _cached_config, _cached_path
    path = _allowlist_path()
    if not force_reload and _cached_config is not None and _cached_path == path:
        return _cached_config

    if not path.is_file():
        raise FileNotFoundError(
            f"windows_fs allow-list not found: {path}. Set {_ENV_ALLOWLIST_PATH} to override."
        )
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    expanded = expand_in_tree(raw)
    parsed = _parse_raw(expanded if isinstance(expanded, dict) else {})
    roots = _merge_env_extra_roots(parsed.allowed_roots)

    config = WindowsFsConfig(
        allowed_roots=tuple(roots),
        denied_patterns=tuple(parsed.denied_patterns),
        allowed_extensions=tuple(parsed.allowed_extensions),
        max_read_bytes=parsed.max_read_bytes,
    )
    _cached_config = config
    _cached_path = path
    return config


def reset_cache() -> None:
    """Drop the cached config; useful for tests."""

    global _cached_config, _cached_path
    _cached_config = None
    _cached_path = None


__all__ = ["WindowsFsConfig", "load_config", "reset_cache"]
