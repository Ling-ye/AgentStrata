"""Shared models and deterministic helpers for Component Catalog audits."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from types import ModuleType


_COMPONENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CatalogRecords = Iterable[tuple[object, object]] | Mapping[object, object]
ModuleLoader = Callable[[str], ModuleType]


@dataclass(frozen=True)
class CatalogAuditIssue:
    """One stable, machine-readable consistency failure."""

    code: str
    message: str
    surface: str
    component: str = ""
    module: str = ""
    tool: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogAuditStats:
    tool_packs: int = 0
    tool_features: int = 0
    mcp_entries: int = 0
    subagents: int = 0
    workflows: int = 0
    tool_modules: int = 0
    static_tools: int = 0


@dataclass(frozen=True)
class CatalogAuditReport:
    """Complete audit result and deterministic aggregate counts."""

    issues: tuple[CatalogAuditIssue, ...]
    stats: CatalogAuditStats

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ok": self.ok,
            "issue_count": len(self.issues),
            "stats": asdict(self.stats),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class _ToolPackAuditFacts:
    pack_count: int
    module_count: int
    tool_names: frozenset[str]


def _records(value: CatalogRecords) -> list[tuple[object, object]]:
    items = value.items() if isinstance(value, Mapping) else value
    return sorted(list(items), key=lambda item: (repr(item[0]), repr(type(item[1]))))


def _component_label(value: object) -> str:
    return value if isinstance(value, str) else repr(value)


def _valid_component_id(value: object) -> bool:
    return isinstance(value, str) and _COMPONENT_ID_RE.fullmatch(value) is not None


def _append(
    issues: list[CatalogAuditIssue],
    code: str,
    message: str,
    *,
    surface: str,
    component: str = "",
    module: str = "",
    tool: str = "",
) -> None:
    issues.append(
        CatalogAuditIssue(
            code=code,
            message=message,
            surface=surface,
            component=component,
            module=module,
            tool=tool,
        )
    )


def _json_serializable(value: object) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        return False
    return True

__all__ = [
    "CatalogAuditIssue",
    "CatalogAuditReport",
    "CatalogAuditStats",
]
