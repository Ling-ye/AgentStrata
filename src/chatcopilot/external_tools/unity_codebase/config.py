"""Load and validate the Unity project registry."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

from chatcopilot.external_tools.shared.env_template import expand_in_tree

_DEFAULT_PROJECTS_FILENAME = "projects.yaml"
_ENV_PROJECTS_PATH = "CHATCOPILOT_UNITY_PROJECTS"
_DEFAULT_PROJECT_ID = "sample_game"
_MAX_READ_BYTES_DEFAULT = 1_048_576


@dataclass(frozen=True)
class UnityProjectConfig:
    """Resolved configuration for a single Unity project."""

    project_id: str
    display_name: str
    root: Path
    allow_globs: tuple[str, ...]
    deny_globs: tuple[str, ...]
    allow_extensions: tuple[str, ...]
    skills: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    max_read_bytes: int = _MAX_READ_BYTES_DEFAULT


@dataclass(frozen=True)
class UnityProjectRegistry:
    """All registered Unity projects, keyed by logical project id."""

    projects: Dict[str, UnityProjectConfig]
    default_id: str = _DEFAULT_PROJECT_ID

    def get(self, project_id: str | None = None) -> UnityProjectConfig:
        key = project_id or self.default_id
        if key not in self.projects:
            available = ", ".join(sorted(self.projects)) or "<none>"
            raise KeyError(f"unknown unity project: {key!r}; available: {available}")
        return self.projects[key]

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.projects))


_cached: Optional[UnityProjectRegistry] = None
_cached_path: Optional[Path] = None


def _projects_path() -> Path:
    override = os.environ.get(_ENV_PROJECTS_PATH)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / _DEFAULT_PROJECTS_FILENAME


def _normalize_extension(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if not ext or ext.startswith(".") else f".{ext}"


def _parse_project(project_id: str, data: dict) -> UnityProjectConfig:
    root_raw = str(data.get("root") or "").strip()
    if not root_raw:
        raise ValueError(f"project {project_id!r} missing required field: root")

    allow_globs = [str(item) for item in (data.get("allow_globs") or []) if str(item).strip()]
    deny_globs = [str(item) for item in (data.get("deny_globs") or []) if str(item).strip()]
    allow_extensions = [
        _normalize_extension(str(item)) for item in (data.get("allow_extensions") or [])
    ]
    skills_raw = data.get("skills") or {}
    if not isinstance(skills_raw, dict):
        raise ValueError(f"project {project_id!r} field 'skills' must be a mapping")
    skills = {str(k): str(v) for k, v in skills_raw.items() if str(v).strip()}

    max_read_bytes = int(data.get("max_read_bytes") or _MAX_READ_BYTES_DEFAULT)

    return UnityProjectConfig(
        project_id=project_id,
        display_name=str(data.get("display_name") or project_id),
        root=Path(root_raw).expanduser(),
        allow_globs=tuple(allow_globs),
        deny_globs=tuple(deny_globs),
        allow_extensions=tuple(allow_extensions),
        skills=skills,
        description=str(data.get("description") or "").strip(),
        max_read_bytes=max_read_bytes,
    )


def load_registry(*, force_reload: bool = False) -> UnityProjectRegistry:
    """Load ``projects.yaml`` from disk, expand env templates, cache the result."""

    global _cached, _cached_path
    path = _projects_path()
    if not force_reload and _cached is not None and _cached_path == path:
        return _cached

    if not path.is_file():
        raise FileNotFoundError(
            f"unity projects registry not found: {path}. Set {_ENV_PROJECTS_PATH} to override."
        )
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    expanded = expand_in_tree(raw)
    if not isinstance(expanded, dict):
        raise ValueError(f"invalid projects.yaml shape (expected mapping): {path}")

    projects_node = expanded.get("projects") or {}
    if not isinstance(projects_node, dict) or not projects_node:
        raise ValueError(f"projects.yaml must declare at least one project under 'projects': {path}")

    projects: Dict[str, UnityProjectConfig] = {}
    for project_id, project_data in projects_node.items():
        if not isinstance(project_data, dict):
            raise ValueError(f"project {project_id!r} entry must be a mapping")
        projects[str(project_id)] = _parse_project(str(project_id), project_data)

    default_id = str(expanded.get("default") or _DEFAULT_PROJECT_ID)
    if default_id not in projects:
        default_id = next(iter(projects))

    registry = UnityProjectRegistry(projects=projects, default_id=default_id)
    _cached = registry
    _cached_path = path
    return registry


def reset_cache() -> None:
    """Drop the cached registry; useful for tests."""

    global _cached, _cached_path
    _cached = None
    _cached_path = None


__all__ = [
    "UnityProjectConfig",
    "UnityProjectRegistry",
    "load_registry",
    "reset_cache",
]
