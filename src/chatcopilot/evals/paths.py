"""Canonical path boundaries for managed and standalone Evaluations."""

from __future__ import annotations

import os
from pathlib import Path


def _is_repository_root(path: Path) -> bool:
    pyproject = path / "pyproject.toml"
    package = path / "src" / "chatcopilot"
    return (
        path.is_dir()
        and not path.is_symlink()
        and pyproject.is_file()
        and not pyproject.is_symlink()
        and package.is_dir()
        and not package.is_symlink()
    )


def _validated_repository_root(path: Path, *, source: str) -> Path:
    raw = path.expanduser()
    if raw.is_symlink():
        raise RuntimeError(f"{source} cannot be a symlink")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{source} does not resolve to a repository") from exc
    if not _is_repository_root(resolved):
        raise RuntimeError(f"{source} must contain pyproject.toml and src/chatcopilot")
    return resolved


def _discover_repository_root() -> Path:
    anchors = (Path.cwd(), Path(__file__).resolve().parent)
    visited: set[Path] = set()
    for anchor in anchors:
        current = anchor if anchor.is_dir() else anchor.parent
        for candidate in (current, *current.parents):
            resolved = candidate.resolve()
            if resolved in visited:
                continue
            visited.add(resolved)
            if _is_repository_root(resolved):
                return resolved
    raise RuntimeError(
        "cannot locate a trusted AgentStrata repository root; set CHATCOPILOT_SOURCE_ROOT"
    )


def _repository_root(repository_root: Path | None) -> Path:
    if repository_root is not None:
        return _validated_repository_root(
            repository_root,
            source="repository_root",
        )
    configured = os.environ.get("CHATCOPILOT_SOURCE_ROOT", "").strip()
    if configured:
        return _validated_repository_root(
            Path(configured),
            source="CHATCOPILOT_SOURCE_ROOT",
        )
    return _discover_repository_root()


def managed_evaluation_root(repository_root: Path | None = None) -> Path:
    """Return the configured service-owned artifact root."""

    base = _repository_root(repository_root)
    configured = os.environ.get("CHATCOPILOT_EVALUATION_ROOT", "").strip()
    if configured:
        raw = Path(configured).expanduser()
        value = raw if raw.is_absolute() else base / raw
        return value.absolute()
    return (base / "reports" / "evals" / "evaluations").absolute()


def is_managed_evaluation_output(
    output: Path,
    *,
    repository_root: Path | None = None,
) -> bool:
    """Return whether output is contained by the service-owned root."""

    candidate = output.expanduser().resolve()
    root = managed_evaluation_root(repository_root).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = ["is_managed_evaluation_output", "managed_evaluation_root"]
