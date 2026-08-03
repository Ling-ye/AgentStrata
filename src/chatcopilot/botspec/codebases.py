"""Validation helpers for bot-owned codebase registries."""
from __future__ import annotations

from chatcopilot.botspec.model import BotSpec, ValidationIssue
from chatcopilot.external_tools.codebase.config import load_registry


def validate_codebase_registry(spec: BotSpec) -> list[ValidationIssue]:
    path = spec.resolve_path(spec.context.codebases.registry)
    enabled = set(spec.tools.packs)
    codebase_enabled = "codebase.read" in enabled
    if path is None:
        if codebase_enabled:
            return [
                ValidationIssue(
                    "error",
                    "启用代码仓库能力时必须配置 codebases.registry",
                    "context.codebases.registry",
                )
            ]
        return []
    if not path.is_file():
        return []
    try:
        load_registry(path, force_reload=True)
    except Exception as exc:  # noqa: BLE001
        return [ValidationIssue("error", f"代码仓库注册表无效: {exc}", "context.codebases.registry")]
    return []


__all__ = ["validate_codebase_registry"]
