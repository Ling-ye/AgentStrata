"""Feature, MCP, subagent, and workflow Component Catalog audits."""

from __future__ import annotations

from chatcopilot.component_catalog.audit_models import (
    CatalogAuditIssue,
    CatalogRecords,
    _TOOL_NAME_RE,
    _append,
    _component_label,
    _json_serializable,
    _records,
    _valid_component_id,
)
from chatcopilot.contracts.subagents import SubagentDef, WorkflowDef
from chatcopilot.contracts.tool_packs import ToolFeatureEntry
from chatcopilot.core.mcp_catalog import McpCatalogEntry


_MCP_TRANSPORTS = frozenset({"stdio", "sse", "streamable_http"})
_MCP_EXPOSURES = frozenset({"subagent", "main", "disabled"})
_MCP_RISKS = frozenset({"search", "readonly", "interactive", "write"})


def _audit_features(records: CatalogRecords, issues: list[CatalogAuditIssue]) -> int:
    count = 0
    for raw_key, raw_entry in _records(records):
        feature = _component_label(raw_key)
        if not _valid_component_id(raw_key):
            _append(
                issues,
                "tool_feature.key_invalid",
                "Tool-feature keys must use a stable lowercase component id.",
                surface="tool_feature",
                component=feature,
            )
        if not isinstance(raw_entry, ToolFeatureEntry):
            _append(
                issues,
                "tool_feature.entry_invalid",
                "Tool-feature values must be ToolFeatureEntry objects.",
                surface="tool_feature",
                component=feature,
            )
            continue
        count += 1
        if raw_entry.name != raw_key:
            _append(
                issues,
                "tool_feature.name_mismatch",
                "ToolFeatureEntry.name must match its catalog key.",
                surface="tool_feature",
                component=feature,
            )
        if not isinstance(raw_entry.description, str) or not raw_entry.description.strip():
            _append(
                issues,
                "tool_feature.description_invalid",
                "ToolFeatureEntry.description must be non-empty.",
                surface="tool_feature",
                component=feature,
            )
    return count


def _audit_mcp(
    records: CatalogRecords,
    *,
    known_subagents: frozenset[str],
    issues: list[CatalogAuditIssue],
) -> int:
    count = 0
    server_ids: dict[str, str] = {}
    for raw_key, raw_entry in _records(records):
        component = _component_label(raw_key)
        if not _valid_component_id(raw_key):
            _append(
                issues,
                "mcp.key_invalid",
                "MCP catalog keys must use a stable lowercase component id.",
                surface="mcp",
                component=component,
            )
        if not isinstance(raw_entry, McpCatalogEntry):
            _append(
                issues,
                "mcp.entry_invalid",
                "MCP catalog values must be McpCatalogEntry objects.",
                surface="mcp",
                component=component,
            )
            continue
        entry = raw_entry
        count += 1
        if entry.id != raw_key:
            _append(
                issues,
                "mcp.name_mismatch",
                "McpCatalogEntry.id must match its catalog key.",
                surface="mcp",
                component=component,
            )
        if not isinstance(entry.title, str) or not entry.title.strip():
            _append(
                issues,
                "mcp.title_invalid",
                "MCP catalog titles must be non-empty.",
                surface="mcp",
                component=component,
            )
        if not isinstance(entry.source_url, str) or not entry.source_url.startswith(
            "https://"
        ):
            _append(
                issues,
                "mcp.source_url_invalid",
                "MCP source_url must use HTTPS.",
                surface="mcp",
                component=component,
            )
        server = entry.server
        if not isinstance(server, dict):
            _append(
                issues,
                "mcp.server_invalid",
                "MCP server must be a mapping.",
                surface="mcp",
                component=component,
            )
            continue
        server_id = str(server.get("id") or "").strip()
        if not server_id:
            _append(
                issues,
                "mcp.server_id_invalid",
                "MCP server id must be non-empty.",
                surface="mcp",
                component=component,
            )
        elif server_id in server_ids:
            _append(
                issues,
                "mcp.server_id_duplicate",
                "MCP server ids must be unique across catalog entries.",
                surface="mcp",
                component=component,
            )
        else:
            server_ids[server_id] = component
        transport = str(server.get("transport") or "stdio").strip()
        exposure = str(server.get("exposure") or "subagent").strip()
        risk = str(server.get("risk") or entry.risk).strip()
        if transport not in _MCP_TRANSPORTS:
            _append(
                issues,
                "mcp.transport_invalid",
                "MCP transport is not supported.",
                surface="mcp",
                component=component,
            )
        if exposure not in _MCP_EXPOSURES:
            _append(
                issues,
                "mcp.exposure_invalid",
                "MCP exposure is not supported.",
                surface="mcp",
                component=component,
            )
        if (
            not isinstance(entry.risk, str)
            or risk not in _MCP_RISKS
            or entry.risk != risk
        ):
            _append(
                issues,
                "mcp.risk_mismatch",
                "MCP entry and server risk must match a supported value.",
                surface="mcp",
                component=component,
            )
        allowed = server.get("allowed_subagents", [])
        if not isinstance(allowed, list) or any(
            not isinstance(name, str) or not name.strip() for name in allowed
        ):
            _append(
                issues,
                "mcp.allowed_subagents_invalid",
                "MCP allowed_subagents must be a list of non-empty names.",
                surface="mcp",
                component=component,
            )
        else:
            for name in sorted(set(allowed) - known_subagents):
                _append(
                    issues,
                    "mcp.subagent_unknown",
                    "MCP allowed_subagents must reference a built-in catalog preset.",
                    surface="mcp",
                    component=component,
                    tool=name,
                )
        search_tools = server.get("search_only_tools", [])
        if risk == "search" and (
            not isinstance(search_tools, list)
            or not search_tools
            or any(not isinstance(name, str) or not name.strip() for name in search_tools)
            or len(set(search_tools)) != len(search_tools)
        ):
            _append(
                issues,
                "mcp.search_tools_invalid",
                "Search MCP entries must declare unique non-empty search_only_tools.",
                surface="mcp",
                component=component,
            )
    return count


def _audit_subagents_and_workflows(
    subagent_records: CatalogRecords,
    workflow_records: CatalogRecords,
    *,
    static_tool_names: frozenset[str],
    issues: list[CatalogAuditIssue],
) -> tuple[int, int, frozenset[str]]:
    subagents: dict[str, SubagentDef] = {}
    delegate_owners: dict[str, str] = {}
    for raw_key, raw_entry in _records(subagent_records):
        component = _component_label(raw_key)
        if not _valid_component_id(raw_key):
            _append(
                issues,
                "subagent.key_invalid",
                "Subagent keys must use a stable lowercase component id.",
                surface="subagent",
                component=component,
            )
        if not isinstance(raw_entry, SubagentDef):
            _append(
                issues,
                "subagent.entry_invalid",
                "Subagent catalog values must be SubagentDef objects.",
                surface="subagent",
                component=component,
            )
            continue
        subagents[component] = raw_entry
        if raw_entry.name != raw_key:
            _append(
                issues,
                "subagent.name_mismatch",
                "SubagentDef.name must match its catalog key.",
                surface="subagent",
                component=component,
            )
        tool_name = raw_entry.tool_name
        valid_tool_name = (
            isinstance(tool_name, str)
            and _TOOL_NAME_RE.fullmatch(tool_name) is not None
        )
        if not valid_tool_name:
            _append(
                issues,
                "subagent.tool_name_invalid",
                "Subagent tool_name must be a valid static tool name.",
                surface="subagent",
                component=component,
                tool=tool_name if isinstance(tool_name, str) else "",
            )
        if (
            not isinstance(raw_entry.summary, str)
            or not raw_entry.summary.strip()
            or not isinstance(raw_entry.role_prompt, str)
            or not raw_entry.role_prompt.strip()
        ):
            _append(
                issues,
                "subagent.text_invalid",
                "Subagent summary and role_prompt must be non-empty.",
                surface="subagent",
                component=component,
            )
        if not _json_serializable((raw_entry.input_schema, raw_entry.output_schema)):
            _append(
                issues,
                "subagent.schema_invalid",
                "Subagent input/output schemas must be JSON-serializable.",
                surface="subagent",
                component=component,
            )
        if not valid_tool_name:
            continue
        previous = delegate_owners.get(tool_name)
        if previous is not None or tool_name in static_tool_names:
            _append(
                issues,
                "delegate.tool_name_conflict",
                "Static and delegated tool names must be globally unique.",
                surface="subagent",
                component=component,
                tool=tool_name,
            )
        else:
            delegate_owners[tool_name] = component

    workflows: dict[str, WorkflowDef] = {}
    for raw_key, raw_entry in _records(workflow_records):
        component = _component_label(raw_key)
        if not _valid_component_id(raw_key):
            _append(
                issues,
                "workflow.key_invalid",
                "Workflow keys must use a stable lowercase component id.",
                surface="workflow",
                component=component,
            )
        if not isinstance(raw_entry, WorkflowDef):
            _append(
                issues,
                "workflow.entry_invalid",
                "Workflow catalog values must be WorkflowDef objects.",
                surface="workflow",
                component=component,
            )
            continue
        workflows[component] = raw_entry
        if raw_entry.name != raw_key:
            _append(
                issues,
                "workflow.name_mismatch",
                "WorkflowDef.name must match its catalog key.",
                surface="workflow",
                component=component,
            )
        tool_name = raw_entry.tool_name
        valid_tool_name = (
            isinstance(tool_name, str)
            and _TOOL_NAME_RE.fullmatch(tool_name) is not None
        )
        if not valid_tool_name:
            _append(
                issues,
                "workflow.tool_name_invalid",
                "Workflow tool_name must be a valid static tool name.",
                surface="workflow",
                component=component,
                tool=tool_name if isinstance(tool_name, str) else "",
            )
        if not isinstance(raw_entry.summary, str) or not raw_entry.summary.strip():
            _append(
                issues,
                "workflow.summary_invalid",
                "Workflow summary must be non-empty.",
                surface="workflow",
                component=component,
            )
        steps_valid = (
            isinstance(raw_entry.steps, tuple)
            and bool(raw_entry.steps)
            and all(isinstance(step, str) and step in subagents for step in raw_entry.steps)
        )
        if not steps_valid:
            _append(
                issues,
                "workflow.steps_invalid",
                "Workflow steps must be non-empty references to catalog subagents.",
                surface="workflow",
                component=component,
            )
        optional_steps_valid = (
            isinstance(raw_entry.optional_steps, tuple)
            and all(
                isinstance(step, str)
                and isinstance(raw_entry.steps, tuple)
                and step in raw_entry.steps
                for step in raw_entry.optional_steps
            )
        )
        if not optional_steps_valid:
            _append(
                issues,
                "workflow.optional_steps_invalid",
                "Workflow optional_steps must be included in steps.",
                surface="workflow",
                component=component,
            )
        retry_map_valid = (
            isinstance(raw_entry.retry_map, tuple)
            and all(
                isinstance(retry_pair, tuple)
                and len(retry_pair) == 2
                and all(isinstance(name, str) for name in retry_pair)
                for retry_pair in raw_entry.retry_map
            )
        )
        retry_names = (
            {
                name
                for retry_pair in raw_entry.retry_map
                for name in retry_pair
            }
            if retry_map_valid
            else set()
        )
        if not retry_map_valid or not isinstance(raw_entry.steps, tuple) or any(
            name not in raw_entry.steps for name in retry_names
        ):
            _append(
                issues,
                "workflow.retry_map_invalid",
                "Workflow retry_map names must be included in steps.",
                surface="workflow",
                component=component,
            )
        if not valid_tool_name:
            continue
        previous = delegate_owners.get(tool_name)
        if previous is not None or tool_name in static_tool_names:
            _append(
                issues,
                "delegate.tool_name_conflict",
                "Static and delegated tool names must be globally unique.",
                surface="workflow",
                component=component,
                tool=tool_name,
            )
        else:
            delegate_owners[tool_name] = component

    return len(subagents), len(workflows), frozenset(subagents)
