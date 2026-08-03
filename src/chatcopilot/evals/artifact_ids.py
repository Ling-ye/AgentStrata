"""Stable, path-safe identifiers for Evaluation artifacts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def safe_artifact_component(value: object) -> str:
    """Return a stable filename component without trusting external identifiers."""

    raw = str(value)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    slug = _UNSAFE_COMPONENT.sub("-", raw).strip("._-")[:48].rstrip("._-")
    return f"{slug or 'artifact'}-{digest}"


def trial_artifact_id(
    case_id: object,
    *,
    attempt: int,
    target_fingerprint: object,
) -> str:
    """Build a stable Trial id while keeping the original Case id in its own field."""

    case_component = safe_artifact_component(case_id)
    target_component = safe_artifact_component(target_fingerprint)[-16:]
    return f"{case_component}-a{attempt}-{target_component}"


def contained_artifact_path(root: Path, *components: str) -> Path:
    """Resolve an artifact path and reject symlink or component escapes."""

    boundary = root.expanduser().resolve()
    candidate = boundary.joinpath(*components).resolve()
    if not candidate.is_relative_to(boundary):
        raise ValueError("Evaluation artifact path escapes its output directory")
    return candidate


__all__ = [
    "contained_artifact_path",
    "safe_artifact_component",
    "trial_artifact_id",
]
