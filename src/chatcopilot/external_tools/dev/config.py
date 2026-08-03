"""DevConfig: project root, path safety rules, shell constraints.

Loaded from environment variables set by BotSpec ``context.dev``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from chatcopilot.project import ENV_PREFIX

_ENV_DEV_ROOT = f"{ENV_PREFIX}_DEV_ROOT"
_ENV_DEV_ALLOWED = f"{ENV_PREFIX}_DEV_ALLOWED_PATHS"
_ENV_DEV_DENIED = f"{ENV_PREFIX}_DEV_DENIED_PATHS"
_ENV_DEV_PROTECTED_BRANCHES = f"{ENV_PREFIX}_DEV_PROTECTED_BRANCHES"
_ENV_DEV_SHELL_TIMEOUT_MAX = f"{ENV_PREFIX}_DEV_SHELL_TIMEOUT_MAX"

_FALLBACK_ROOT_ENV = f"{ENV_PREFIX}_CODEBASE_CHATCOPILOT_ROOT"

_DEFAULT_DENIED_PATHS: tuple[str, ...] = (
    "**/.git/**",
    "**/local.env",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa*",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/credentials*",
)

_DEFAULT_PROTECTED_BRANCHES: tuple[str, ...] = ("main", "master")

_BLOCKED_SHELL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bsudo\s+rm\b"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+.*of=/dev/"),
)


@dataclass(frozen=True)
class ShellConfig:
    timeout_default: int = 60
    timeout_max: int = 300
    max_output_chars: int = 30_000
    blocked_patterns: tuple[re.Pattern[str], ...] = _BLOCKED_SHELL_PATTERNS


@dataclass(frozen=True)
class DevConfig:
    repo_root: Path
    allowed_paths: tuple[str, ...] = ("**",)
    denied_paths: tuple[str, ...] = _DEFAULT_DENIED_PATHS
    protected_branches: tuple[str, ...] = _DEFAULT_PROTECTED_BRANCHES
    shell: ShellConfig = field(default_factory=ShellConfig)

    @classmethod
    def from_env(cls) -> "DevConfig":
        root_raw = os.environ.get(_ENV_DEV_ROOT, "").strip()
        if not root_raw:
            root_raw = os.environ.get(_FALLBACK_ROOT_ENV, "").strip()
        if not root_raw:
            raise RuntimeError(
                f"dev workspace root not configured; set {_ENV_DEV_ROOT} or {_FALLBACK_ROOT_ENV}"
            )
        repo_root = Path(root_raw).expanduser().resolve()
        if not repo_root.is_dir():
            raise RuntimeError(f"dev workspace root is not a directory: {repo_root}")

        allowed = _parse_path_list(os.environ.get(_ENV_DEV_ALLOWED, ""))
        denied = _parse_path_list(os.environ.get(_ENV_DEV_DENIED, ""))
        protected = _parse_csv(os.environ.get(_ENV_DEV_PROTECTED_BRANCHES, ""))
        timeout_max = _parse_int(os.environ.get(_ENV_DEV_SHELL_TIMEOUT_MAX, ""), default=300)

        return cls(
            repo_root=repo_root,
            allowed_paths=allowed or ("**",),
            denied_paths=tuple(dict.fromkeys((*_DEFAULT_DENIED_PATHS, *denied))),
            protected_branches=protected or _DEFAULT_PROTECTED_BRANCHES,
            shell=ShellConfig(timeout_max=max(60, timeout_max)),
        )


_cached: DevConfig | None = None


def get_dev_config(*, force_reload: bool = False) -> DevConfig:
    global _cached
    if _cached is None or force_reload:
        _cached = DevConfig.from_env()
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None


def _parse_path_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_int(value: str | None, *, default: int) -> int:
    if not value or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


__all__ = ["DevConfig", "ShellConfig", "get_dev_config", "reset_cache"]
