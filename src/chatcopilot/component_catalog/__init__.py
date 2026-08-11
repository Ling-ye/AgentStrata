"""Stable component catalog API for control surfaces."""

from chatcopilot.component_catalog.audit import (
    CatalogAuditIssue,
    CatalogAuditReport,
    CatalogAuditStats,
    audit_component_catalog,
)
from chatcopilot.component_catalog.catalog import (
    CatalogProjectionError,
    get_mcp_catalog_entry,
    get_tool_feature_entry,
    get_tool_pack_entry,
    iter_mcp_catalog_entries,
    iter_subagent_presets,
    iter_tool_features,
    iter_tool_pack_tools,
    iter_tool_packs,
    iter_workflows,
)
from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS, PRESET_NAMES

__all__ = [
    "BUILTIN_SUBAGENTS",
    "CatalogAuditIssue",
    "CatalogAuditReport",
    "CatalogAuditStats",
    "CatalogProjectionError",
    "PRESET_NAMES",
    "audit_component_catalog",
    "get_mcp_catalog_entry",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "iter_mcp_catalog_entries",
    "iter_subagent_presets",
    "iter_tool_features",
    "iter_tool_pack_tools",
    "iter_tool_packs",
    "iter_workflows",
]
