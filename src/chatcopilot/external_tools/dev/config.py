"""DevConfig: project root, path safety rules, shell constraints.

Loaded from environment variables set by BotSpec ``context.dev``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from chatcopilot.core.config import load_command_timeouts
from chatcopilot.project import ENV_PREFIX

_ENV_DEV_ROOT = f"{ENV_PREFIX}_DEV_ROOT"
_ENV_DEV_ALLOWED = f"{ENV_PREFIX}_DEV_ALLOWED_PATHS"
_ENV_DEV_DENIED = f"{ENV_PREFIX}_DEV_DENIED_PATHS"

_FALLBACK_ROOT_ENV = f"{ENV_PREFIX}_CODEBASE_CHATCOPILOT_ROOT"

# Unbound trusted delivery still uses this policy. Interactive tools get their
# path permissions from ExecutionScope instead.
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

@dataclass(frozen=True)
class ShellConfig:
    timeout_default: int = 60
    timeout_max: int = 300


@dataclass(frozen=True)
class DevConfig:
    repo_root: Path
    allowed_paths: tuple[str, ...] = ("**",)
    denied_paths: tuple[str, ...] = _DEFAULT_DENIED_PATHS
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
        timeouts = load_command_timeouts(environment=dict(os.environ))

        return cls(
            repo_root=repo_root,
            allowed_paths=allowed or ("**",),
            denied_paths=tuple(dict.fromkeys((*_DEFAULT_DENIED_PATHS, *denied))),
            shell=ShellConfig(
                timeout_default=timeouts.timeout_default,
                timeout_max=timeouts.timeout_max,
            ),
        )


_cached: DevConfig | None = None


def get_dev_config(*, force_reload: bool = False, require_scope: bool = False) -> DevConfig:
    global _cached
    from chatcopilot.contracts.execution_scope import current_execution_scope

    scope = current_execution_scope()
    if scope is not None:
        return DevConfig(
            repo_root=(scope.project_roots or scope.readable_roots)[0],
            allowed_paths=("**",),
            denied_paths=(),
            shell=ShellConfig(
                timeout_default=scope.command_timeouts.timeout_default,
                timeout_max=scope.command_timeouts.timeout_max,
            ),
        )
    if require_scope:
        raise RuntimeError("execution resources are not bound to this tool call")
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


__all__ = ["DevConfig", "ShellConfig", "get_dev_config", "reset_cache"]
