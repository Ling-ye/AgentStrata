"""Deterministic integrity audit for the static Component Catalog."""

from __future__ import annotations

import importlib

from chatcopilot.component_catalog.audit_models import (
    CatalogAuditIssue,
    CatalogAuditReport,
    CatalogAuditStats,
    CatalogRecords,
    ModuleLoader,
    _append,
)
from chatcopilot.component_catalog.audit_surfaces import (
    _audit_features,
    _audit_mcp,
    _audit_subagents_and_workflows,
)
from chatcopilot.component_catalog.audit_tools import _audit_tool_packs
from chatcopilot.component_catalog.catalog import (
    iter_mcp_catalog_entries,
    iter_subagent_presets,
    iter_tool_features,
    iter_tool_packs,
    iter_workflows,
)


def audit_component_catalog(
    *,
    tool_packs: CatalogRecords | None = None,
    tool_features: CatalogRecords | None = None,
    mcp_entries: CatalogRecords | None = None,
    subagents: CatalogRecords | None = None,
    workflows: CatalogRecords | None = None,
    module_loader: ModuleLoader = importlib.import_module,
) -> CatalogAuditReport:
    """Audit all static component surfaces without executing handlers or remote calls."""

    issues: list[CatalogAuditIssue] = []
    pack_records = list(iter_tool_packs()) if tool_packs is None else tool_packs
    feature_records = list(iter_tool_features()) if tool_features is None else tool_features
    subagent_records = list(iter_subagent_presets()) if subagents is None else subagents
    workflow_records = list(iter_workflows()) if workflows is None else workflows
    if mcp_entries is None:
        try:
            mcp_records: CatalogRecords = list(
                iter_mcp_catalog_entries(use_env_override=False, strict=True)
            )
        except Exception as exc:  # noqa: BLE001
            mcp_records = ()
            _append(
                issues,
                "mcp.catalog_load_failed",
                f"Packaged MCP catalog load failed: {type(exc).__name__}.",
                surface="mcp",
            )
    else:
        mcp_records = mcp_entries

    tool_facts = _audit_tool_packs(
        pack_records,
        module_loader=module_loader,
        issues=issues,
    )
    feature_count = _audit_features(feature_records, issues)
    subagent_count, workflow_count, known_subagents = _audit_subagents_and_workflows(
        subagent_records,
        workflow_records,
        static_tool_names=tool_facts.tool_names,
        issues=issues,
    )
    mcp_count = _audit_mcp(
        mcp_records,
        known_subagents=known_subagents,
        issues=issues,
    )

    issues.sort(
        key=lambda issue: (
            issue.surface,
            issue.component,
            issue.module,
            issue.tool,
            issue.code,
            issue.message,
        )
    )
    return CatalogAuditReport(
        issues=tuple(issues),
        stats=CatalogAuditStats(
            tool_packs=tool_facts.pack_count,
            tool_features=feature_count,
            mcp_entries=mcp_count,
            subagents=subagent_count,
            workflows=workflow_count,
            tool_modules=tool_facts.module_count,
            static_tools=len(tool_facts.tool_names),
        ),
    )


__all__ = [
    "CatalogAuditIssue",
    "CatalogAuditReport",
    "CatalogAuditStats",
    "audit_component_catalog",
]
