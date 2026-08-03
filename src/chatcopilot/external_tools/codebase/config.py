"""Load the bot-owned codebase repository registry."""
from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from chatcopilot.external_tools.shared.env_template import expand_in_tree
from chatcopilot.project import ENV_PREFIX

_ENV_REGISTRY = f"{ENV_PREFIX}_CODEBASE_REGISTRY"
_ENV_CACHE_ROOT = f"{ENV_PREFIX}_CODEBASE_CACHE_ROOT"
_REPOSITORY_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_DEFAULT_MAX_READ_BYTES = 1_048_576
_DEFAULT_EXTENSIONS = (
    ".asmdef", ".c", ".cc", ".cfg", ".cpp", ".cs", ".csproj", ".go",
    ".h", ".hpp", ".ini", ".java", ".js", ".json", ".lua", ".md",
    ".props", ".py", ".rs", ".sh", ".sln", ".toml", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml",
)
_DEFAULT_DENY_GLOBS = (
    ".git/**", "**/.git/**", "**/local.env", "**/.env", "**/*.pem",
    "**/*.key", "**/*.p12", "**/*.pfx", "**/id_rsa*", "**/__pycache__/**",
    "**/.venv/**", "**/venv/**", "**/node_modules/**", "**/Library/**",
    "**/Temp/**", "**/Logs/**", "**/Build/**", "**/bin/**", "**/obj/**",
    "**/dist/**", "**/target/**",
)


@dataclass(frozen=True)
class CodeRepositoryConfig:
    repository_id: str
    display_name: str
    root: Path
    description: str = ""
    include_globs: tuple[str, ...] = ("**",)
    deny_globs: tuple[str, ...] = _DEFAULT_DENY_GLOBS
    allow_extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS
    max_read_bytes: int = _DEFAULT_MAX_READ_BYTES
    remote: str | None = None
    base_branch: str = "main"
    write_enabled: bool = False
    branch_prefix: str = "bot"
    write_globs: tuple[str, ...] = ()
    required_docs: tuple[str, ...] = ()
    checks: tuple["CodebaseCheckConfig", ...] = ()


@dataclass(frozen=True)
class CodebaseCheckConfig:
    check_id: str
    argv: tuple[str, ...]
    timeout_seconds: int = 120


@dataclass(frozen=True)
class CodebaseRegistry:
    repositories: Mapping[str, CodeRepositoryConfig]

    def get(self, repository_id: str | None = None) -> CodeRepositoryConfig:
        key = str(repository_id or "").strip()
        if not key:
            if len(self.repositories) == 1:
                return next(iter(self.repositories.values()))
            raise KeyError("repository is required when multiple codebases are registered")
        if key not in self.repositories:
            available = ", ".join(sorted(self.repositories)) or "<none>"
            raise KeyError(f"unknown codebase repository: {key!r}; available: {available}")
        return self.repositories[key]


_cached_registry: CodebaseRegistry | None = None
_cached_path: Path | None = None


def registry_path_from_env() -> Path:
    raw = os.environ.get(_ENV_REGISTRY, "").strip()
    if not raw:
        raise RuntimeError(
            f"codebase registry is not configured; set {_ENV_REGISTRY} from BotSpec codebases.registry"
        )
    return Path(raw).expanduser().resolve()


def codebase_cache_root() -> Path:
    raw = os.environ.get(_ENV_CACHE_ROOT, "").strip()
    return (Path(raw).expanduser() if raw else Path.home() / ".chatcopilot" / "codebases").resolve()


def load_registry(
    path: str | Path | None = None,
    *,
    force_reload: bool = False,
) -> CodebaseRegistry:
    global _cached_path, _cached_registry

    resolved_path = (
        Path(path).expanduser().resolve() if path is not None else registry_path_from_env()
    )
    if not force_reload and _cached_registry is not None and _cached_path == resolved_path:
        return _cached_registry
    if not resolved_path.is_file():
        raise FileNotFoundError(f"codebase registry not found: {resolved_path}")

    with resolved_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    expanded = expand_in_tree(raw)
    if not isinstance(expanded, dict):
        raise ValueError("codebase registry root must be a mapping")
    entries = expanded.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ValueError("codebase registry must contain a non-empty repositories list")

    repositories: dict[str, CodeRepositoryConfig] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"repositories[{index}] must be a mapping")
        repository = _parse_repository(entry, index=index)
        if repository.repository_id in repositories:
            raise ValueError(f"duplicate codebase repository id: {repository.repository_id}")
        repositories[repository.repository_id] = repository

    registry = CodebaseRegistry(repositories=repositories)
    _cached_path = resolved_path
    _cached_registry = registry
    return registry


def reset_cache() -> None:
    global _cached_path, _cached_registry
    _cached_path = None
    _cached_registry = None


def _parse_repository(raw: dict, *, index: int) -> CodeRepositoryConfig:
    repository_id = str(raw.get("id") or "").strip()
    if not _REPOSITORY_ID_RE.fullmatch(repository_id):
        raise ValueError(
            f"repositories[{index}].id must be a lowercase repository identifier"
        )
    root_text = str(raw.get("root") or "").strip()
    if not root_text:
        raise ValueError(f"repositories[{index}].root is required")
    root = Path(root_text).expanduser()
    if not root.is_absolute():
        raise ValueError(
            f"repositories[{index}].root must resolve to an absolute path via an environment variable"
        )

    include_globs = _string_tuple(raw.get("include_globs")) or ("**",)
    deny_globs = tuple(dict.fromkeys((*_DEFAULT_DENY_GLOBS, *_string_tuple(raw.get("deny_globs")))))
    extensions = tuple(_normalize_extension(item) for item in _string_tuple(raw.get("allow_extensions")))
    if not extensions:
        extensions = _DEFAULT_EXTENSIONS
    max_read_bytes = int(raw.get("max_read_bytes") or _DEFAULT_MAX_READ_BYTES)
    if max_read_bytes < 1:
        raise ValueError(f"repositories[{index}].max_read_bytes must be positive")
    checks = _parse_checks(raw.get("checks"), repository_index=index)
    write_enabled = _as_bool(raw.get("write_enabled"))
    if write_enabled and not checks:
        raise ValueError(f"writable repository {repository_id!r} must declare checks")
    write_globs = _string_tuple(raw.get("write_globs"))
    if write_enabled and not write_globs:
        raise ValueError(f"writable repository {repository_id!r} must declare write_globs")
    required_docs = _string_tuple(raw.get("required_docs"))
    _validate_required_docs(
        required_docs,
        write_globs=write_globs,
        repository_index=index,
    )
    base_branch = str(raw.get("base_branch") or "main").strip()
    branch_prefix = str(raw.get("branch_prefix") or "bot").strip().strip("/")
    if not _valid_git_ref(base_branch) or not _valid_git_ref(branch_prefix):
        raise ValueError(f"repositories[{index}] base_branch or branch_prefix is not a safe Git ref")
    remote = str(raw.get("remote") or "").strip() or None
    if remote and remote.startswith("-"):
        raise ValueError(f"repositories[{index}].remote cannot start with '-'")

    return CodeRepositoryConfig(
        repository_id=repository_id,
        display_name=str(raw.get("display_name") or repository_id).strip(),
        root=root.resolve(),
        description=str(raw.get("description") or "").strip(),
        include_globs=include_globs,
        deny_globs=deny_globs,
        allow_extensions=extensions,
        max_read_bytes=max_read_bytes,
        remote=remote,
        base_branch=base_branch,
        write_enabled=write_enabled,
        branch_prefix=branch_prefix,
        write_globs=write_globs,
        required_docs=required_docs,
        checks=checks,
    )


def _parse_checks(value: object, *, repository_index: int) -> tuple[CodebaseCheckConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"repositories[{repository_index}].checks must be a YAML list")
    checks: list[CodebaseCheckConfig] = []
    seen: set[str] = set()
    for check_index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(
                f"repositories[{repository_index}].checks[{check_index}] must be a mapping"
            )
        check_id = str(raw.get("id") or "").strip()
        argv_raw = raw.get("argv")
        if not _REPOSITORY_ID_RE.fullmatch(check_id) or not isinstance(argv_raw, list):
            raise ValueError(
                f"repositories[{repository_index}].checks[{check_index}] requires a valid id and argv list"
            )
        argv = tuple(str(item) for item in argv_raw if str(item))
        if not argv:
            raise ValueError(f"codebase check {check_id!r} argv cannot be empty")
        if check_id in seen:
            raise ValueError(f"duplicate codebase check id: {check_id}")
        seen.add(check_id)
        timeout_seconds = int(raw.get("timeout_seconds") or 120)
        if timeout_seconds < 1:
            raise ValueError(f"codebase check {check_id!r} timeout_seconds must be positive")
        checks.append(CodebaseCheckConfig(check_id, argv, timeout_seconds))
    return tuple(checks)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("codebase glob and extension fields must use YAML lists")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _normalize_extension(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized.startswith(".") else f".{normalized}"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _valid_git_ref(value: str) -> bool:
    if (
        not _GIT_REF_RE.fullmatch(value)
        or ".." in value
        or "@{" in value
        or value.startswith("refs/")
        or value.endswith((".", "/"))
    ):
        return False
    parts = value.split("/")
    return all(
        part
        and not part.startswith(".")
        and not part.endswith(".lock")
        for part in parts
    )


def _validate_required_docs(
    paths: tuple[str, ...],
    *,
    write_globs: tuple[str, ...],
    repository_index: int,
) -> None:
    for path in paths:
        normalized = path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or normalized in {"", "."}:
            raise ValueError(
                f"repositories[{repository_index}].required_docs must use safe relative paths"
            )
        if write_globs and not any(
            fnmatchcase(normalized, pattern.replace("\\", "/"))
            for pattern in write_globs
        ):
            raise ValueError(
                f"required document {normalized!r} is outside repository write_globs"
            )


__all__ = [
    "CodeRepositoryConfig",
    "CodebaseCheckConfig",
    "CodebaseRegistry",
    "load_registry",
    "codebase_cache_root",
    "registry_path_from_env",
    "reset_cache",
]
