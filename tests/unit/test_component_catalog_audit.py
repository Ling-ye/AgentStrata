from __future__ import annotations

import json
from types import ModuleType

import pytest

from chatcopilot.component_catalog.audit import audit_component_catalog
from chatcopilot.contracts.subagents import SubagentDef, WorkflowDef
from chatcopilot.contracts.tool_packs import (
    ToolModuleBinding,
    ToolPackEntry,
    ToolPackPrompt,
)
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.core.mcp_catalog import McpCatalogEntry
from chatcopilot.core import mcp_catalog


def _tool(name: str = "demo_tool", **overrides: object) -> ToolDef:
    values: dict[str, object] = {
        "name": name,
        "summary": "A valid catalog audit tool.",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "handler": lambda _args: ("ok", [], None),
        "category": "tests.catalog",
        "owner": "tests",
    }
    values.update(overrides)
    return ToolDef(**values)  # type: ignore[arg-type]


def _module(name: str, **exports: object) -> ModuleType:
    module = ModuleType(name)
    for export_name, value in exports.items():
        setattr(module, export_name, value)
    return module


def _pack(
    name: str,
    module: str,
    *tool_names: str,
    manifest_module: str | None = None,
    manifest_builder: str = "build_manifest",
) -> ToolPackEntry:
    return ToolPackEntry(
        name=name,
        description=f"Catalog test pack {name}.",
        manifest_module=manifest_module,
        manifest_builder=manifest_builder,
        tool_bindings=(ToolModuleBinding(module, tuple(tool_names)),),
    )


def _audit(
    packs: dict[str, ToolPackEntry],
    modules: dict[str, ModuleType],
    *,
    mcp_entries: dict[str, McpCatalogEntry] | None = None,
    subagents: dict[str, SubagentDef] | None = None,
    workflows: dict[str, WorkflowDef] | None = None,
):
    return audit_component_catalog(
        tool_packs=packs,
        tool_features={},
        mcp_entries=mcp_entries or {},
        subagents=subagents or {},
        workflows=workflows or {},
        module_loader=modules.__getitem__,
    )


def test_current_component_catalog_passes_complete_audit(monkeypatch, tmp_path) -> None:
    override = tmp_path / "local-catalog.yaml"
    override.write_text("servers: invalid\n", encoding="utf-8")
    monkeypatch.setenv("CHATCOPILOT_MCP_CATALOG", str(override))

    report = audit_component_catalog()

    assert report.ok
    assert report.issues == ()
    assert report.stats.tool_packs > 0
    assert report.stats.static_tools > 0
    assert report.stats.mcp_entries > 0
    assert report.to_dict()["schema_version"] == 1


def test_strict_mcp_catalog_loading_rejects_duplicate_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_catalog,
        "_load_catalog_yaml",
        lambda **_kwargs: {
            "servers": [
                {"id": "duplicate", "server": {"id": "first"}},
                {"id": "duplicate", "server": {"id": "second"}},
            ]
        },
    )

    with pytest.raises(ValueError, match="ids must be unique"):
        mcp_catalog.load_mcp_catalog(use_env_override=False, strict=True)


def test_audit_enforces_exact_declared_membership() -> None:
    module_name = "chatcopilot.external_tools.tests.membership_tools"
    module = _module(module_name, TOOLS=[_tool("declared"), _tool("orphan")])
    packs = {
        "tests.membership": _pack(
            "tests.membership",
            module_name,
            "declared",
            "missing",
        )
    }

    report = _audit(packs, {module_name: module})
    codes = {(issue.code, issue.tool) for issue in report.issues}

    assert ("tool_binding.tool_missing", "missing") in codes
    assert ("tool_module.tool_unassigned", "orphan") in codes


def test_shared_module_and_explicit_shared_tool_are_valid_and_loaded_once() -> None:
    module_name = "chatcopilot.external_tools.tests.shared_tools"
    module = _module(
        module_name,
        TOOLS=[_tool("first"), _tool("second"), _tool("shared")],
    )
    calls: list[str] = []

    def load(module_path: str) -> ModuleType:
        calls.append(module_path)
        return {module_name: module}[module_path]

    report = audit_component_catalog(
        tool_packs={
            "tests.first": _pack("tests.first", module_name, "first", "shared"),
            "tests.second": _pack("tests.second", module_name, "second", "shared"),
        },
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
        module_loader=load,
    )

    assert report.ok
    assert calls == [module_name]
    assert report.stats.static_tools == 3


def test_cross_module_tool_name_conflict_is_rejected() -> None:
    first_name = "chatcopilot.external_tools.tests.first_tools"
    second_name = "chatcopilot.external_tools.tests.second_tools"
    modules = {
        first_name: _module(first_name, TOOLS=[_tool("same_name")]),
        second_name: _module(second_name, TOOLS=[_tool("same_name")]),
    }
    report = _audit(
        {
            "tests.first": _pack("tests.first", first_name, "same_name"),
            "tests.second": _pack("tests.second", second_name, "same_name"),
        },
        modules,
    )

    conflicts = [issue for issue in report.issues if issue.code == "tool.name_conflict"]

    assert len(conflicts) == 1
    assert conflicts[0].tool == "same_name"
    assert first_name in conflicts[0].module
    assert second_name in conflicts[0].module


def test_manifest_mapping_and_fragment_type_are_checked() -> None:
    tools_name = "chatcopilot.external_tools.tests.prompt_tools"
    manifest_name = "chatcopilot.external_tools.tests.prompt_manifest"
    builder = lambda: ToolPackPrompt(  # noqa: E731
        name="tests.prompt",
        prompt_fragments="not-a-tuple",  # type: ignore[arg-type]
    )
    modules = {
        tools_name: _module(tools_name, TOOLS=[_tool()]),
        manifest_name: _module(
            manifest_name,
            build_manifest=builder,
            TOOL_PACK_PROMPT_BUILDERS={},
        ),
    }
    report = _audit(
        {
            "tests.prompt": _pack(
                "tests.prompt",
                tools_name,
                "demo_tool",
                manifest_module=manifest_name,
            )
        },
        modules,
    )

    assert {issue.code for issue in report.issues} >= {
        "manifest.mapping_mismatch",
        "manifest.fragments_invalid",
    }


def test_tool_security_contract_and_schema_are_checked_without_calling_handler() -> None:
    calls: list[object] = []

    def handler(args):
        calls.append(args)
        return "ok", [], None

    module_name = "chatcopilot.external_tools.tests.security_tools"
    tool = _tool(
        handler=handler,
        requires_role="owenr",
        properties={
            "query": {
                "type": "string",
                "default": object(),
            }
        },
    )
    module = _module(module_name, TOOLS=[tool])

    report = _audit(
        {"tests.security": _pack("tests.security", module_name, "demo_tool")},
        {module_name: module},
    )

    assert calls == []
    assert {issue.code for issue in report.issues} >= {
        "tool.requires_role_invalid",
        "tool.schema_invalid",
    }


def test_malformed_runtime_values_become_issues_instead_of_crashing() -> None:
    module_name = "chatcopilot.external_tools.tests.malformed_tools"
    malformed_tool = _tool(
        requires_role=[],
        execution_policy=[],
        weight=[],
        artifact_kinds=([],),
    )
    malformed_pack = ToolPackEntry(
        name="tests.malformed",
        description="Malformed runtime values.",
        tool_bindings=(
            ToolModuleBinding(
                module_name,
                ("demo_tool", []),  # type: ignore[arg-type]
            ),
        ),
    )
    malformed_mcp = McpCatalogEntry(
        id="tests-mcp",
        title="Catalog test MCP",
        source_url="https://example.com/mcp",
        server=[],  # type: ignore[arg-type]
    )

    report = _audit(
        {"tests.malformed": malformed_pack},
        {module_name: _module(module_name, TOOLS=[malformed_tool])},
        mcp_entries={"tests-mcp": malformed_mcp},
    )

    assert {issue.code for issue in report.issues} >= {
        "mcp.server_invalid",
        "tool.artifact_kinds_invalid",
        "tool.execution_policy_invalid",
        "tool.requires_role_invalid",
        "tool.weight_invalid",
        "tool_binding.name_invalid",
    }


def test_module_namespace_is_fail_closed_and_exception_text_is_redacted() -> None:
    calls: list[str] = []
    hidden = "".join(("secret", "-import-detail"))

    def load(module_path: str) -> ModuleType:
        calls.append(module_path)
        raise RuntimeError(hidden)

    outside_report = audit_component_catalog(
        tool_packs={
            "tests.outside": _pack("tests.outside", "urllib.request", "demo_tool")
        },
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
        module_loader=load,
    )
    assert calls == []
    assert {issue.code for issue in outside_report.issues} == {
        "tool_binding.module_invalid"
    }

    allowed_name = "chatcopilot.external_tools.tests.import_failure"
    import_report = audit_component_catalog(
        tool_packs={
            "tests.import": _pack("tests.import", allowed_name, "demo_tool")
        },
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
        module_loader=load,
    )
    payload = json.dumps(import_report.to_dict(), ensure_ascii=False)

    assert "tool_module.import_failed" in payload
    assert "RuntimeError" in payload
    assert hidden not in payload


def test_cross_surface_delegate_name_and_workflow_references_are_checked() -> None:
    module_name = "chatcopilot.external_tools.tests.delegate_tools"
    module = _module(module_name, TOOLS=[_tool("delegate_demo")])
    subagent = SubagentDef(
        name="demo_agent",
        tool_name="delegate_demo",
        summary="Delegate a catalog test.",
        system_prompt="Return a structured result.",
    )
    workflow = WorkflowDef(
        name="demo_flow",
        tool_name="run_demo_flow",
        summary="Run a catalog test workflow.",
        steps=("missing_agent",),
    )

    report = _audit(
        {"tests.delegate": _pack("tests.delegate", module_name, "delegate_demo")},
        {module_name: module},
        subagents={"demo_agent": subagent},
        workflows={"demo_flow": workflow},
    )

    assert {issue.code for issue in report.issues} >= {
        "delegate.tool_name_conflict",
        "workflow.steps_invalid",
    }


def test_mcp_allowed_subagent_must_reference_catalog_preset() -> None:
    entry = McpCatalogEntry(
        id="tests-mcp",
        title="Catalog test MCP",
        source_url="https://example.com/mcp",
        risk="readonly",
        server={
            "id": "tests-mcp",
            "transport": "streamable_http",
            "url": "https://example.com/mcp",
            "exposure": "subagent",
            "risk": "readonly",
            "allowed_subagents": ["missing_agent"],
        },
    )

    report = _audit({}, {}, mcp_entries={"tests-mcp": entry})

    assert [issue.code for issue in report.issues] == ["mcp.subagent_unknown"]
