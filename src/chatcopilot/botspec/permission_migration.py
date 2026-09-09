"""Pure, reviewable migration of retired permission configuration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def preview_permission_migration(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    updated = deepcopy(raw)
    removed: list[str] = []
    paths = (
        ("llm", "code", "allowed_roles"),
        ("agents", "codex", "owner_access"),
        ("agents", "codex", "member_access"),
        ("access", "owner_only_project_access"),
        ("context", "dev", "allowed_paths"),
        ("context", "dev", "denied_paths"),
    )
    for path in paths:
        parent = updated
        for key in path[:-1]:
            value = parent.get(key)
            if not isinstance(value, dict):
                break
            parent = value
        else:
            if path[-1] in parent:
                del parent[path[-1]]
                removed.append(".".join(path))
    if updated.get("agents", {}).get("codex") == {}:
        del updated["agents"]["codex"]
    if updated.get("access") == {}:
        del updated["access"]
    return updated, removed
