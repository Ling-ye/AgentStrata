"""Stable, lazily loaded component-catalog API for control surfaces."""
from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CatalogAuditIssue": "chatcopilot.component_catalog.audit",
    "CatalogAuditReport": "chatcopilot.component_catalog.audit",
    "CatalogAuditStats": "chatcopilot.component_catalog.audit",
    "audit_component_catalog": "chatcopilot.component_catalog.audit",
    "CatalogProjectionError": "chatcopilot.component_catalog.catalog",
    "get_mcp_catalog_entry": "chatcopilot.component_catalog.catalog",
    "get_subagent_preset": "chatcopilot.component_catalog.catalog",
    "get_tool_feature_entry": "chatcopilot.component_catalog.catalog",
    "get_tool_pack_entry": "chatcopilot.component_catalog.catalog",
    "get_workflow": "chatcopilot.component_catalog.catalog",
    "iter_mcp_catalog_entries": "chatcopilot.component_catalog.catalog",
    "iter_subagent_presets": "chatcopilot.component_catalog.catalog",
    "iter_tool_features": "chatcopilot.component_catalog.catalog",
    "iter_tool_pack_tools": "chatcopilot.component_catalog.catalog",
    "iter_tool_packs": "chatcopilot.component_catalog.catalog",
    "iter_workflows": "chatcopilot.component_catalog.catalog",
    "known_subagent_preset_names": "chatcopilot.component_catalog.catalog",
    "known_workflow_names": "chatcopilot.component_catalog.catalog",
    "BUILTIN_SUBAGENTS": "chatcopilot.component_catalog.subagents",
    "BUILTIN_SUBAGENT_WORKFLOWS": "chatcopilot.component_catalog.subagents",
    "PRESET_NAMES": "chatcopilot.component_catalog.subagents",
    "WORKFLOW_NAMES": "chatcopilot.component_catalog.subagents",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
