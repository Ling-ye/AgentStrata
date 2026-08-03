"""Shared helpers for external tool ``spec.py`` modules.

The helpers keep each domain package focused on declaring tools while common
argument validation, schema reflection, and workspace path behavior stay in one
place.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from chatcopilot.external_tools.shared.tool_spec import properties_from_argparse


def require_arg(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or value == "":
        raise ValueError(f"缺少必填参数: {key}")
    return str(value)


def normalize_outputs(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [str(value)]


def build_props(
    parser: argparse.ArgumentParser,
    dests: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    return properties_from_argparse(parser, list(dests))


def schema_property(
    *,
    type: str,
    description: str,
    default: Any = None,
    enum: Iterable[Any] | None = None,
) -> Dict[str, Any]:
    prop: Dict[str, Any] = {
        "type": type,
        "description": description,
    }
    if default is not None:
        prop["default"] = default
    if enum is not None:
        prop["enum"] = list(enum)
    return prop


def validate_choice(value: str, choices: Iterable[str], *, name: str) -> str:
    allowed = set(choices)
    if value not in allowed:
        raise ValueError(f"{name} 仅支持 " + " / ".join(sorted(allowed)))
    return value


def validate_non_negative(value: int | float, *, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} 不能小于 0")


def current_workspace(*, create: bool = True):
    """Return the middleware-injected current chat workspace."""
    from chatcopilot.core.workspace_context import resolve_workspace

    return resolve_workspace(create=create)


def resolve_workspace_path(
    raw_path: str,
    *,
    kind: str = "文件",
    must_exist: bool = True,
) -> str:
    """Resolve a path against the current chat workspace when relative.

    The import is intentionally local so pure CLI/module imports do not pull in
    the agent workspace unless a tool handler actually needs session semantics.
    """
    raw_path = str(raw_path).strip()
    if not raw_path:
        raise ValueError(f"{kind}路径不能为空")

    ws = current_workspace(create=True)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = ws.resolve_relative(candidate)
    target = candidate.resolve()
    if not ws.is_inside(target):
        raise PermissionError(f"{kind}路径越出当前用户工作区: {target}")
    if must_exist and not target.exists():
        raise FileNotFoundError(f"{kind}不存在: {ws.relpath(target) if ws.is_inside(target) else target}")
    return str(target)
