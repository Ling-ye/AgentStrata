from __future__ import annotations

import json
from types import ModuleType

import pytest

from chatcopilot.component_catalog.audit import audit_component_catalog
from chatcopilot.contracts.subagents import SubagentDef, WorkflowDef
from chatcopilot.contracts.tool_packs import (
    ToolPackEntry,
    ToolProvider,
)
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.core.mcp_catalog import McpCatalogEntry
from chatcopilot.core import mcp_catalog


def _tool(name: str = "demo_tool", **overrides: object) -> ToolDef:
    values: dict[str, object] = {
        "name": name,
        "summary": "A valid catalog audit tool.",
        "input_schema": object_schema(
            {"query": {"type": "string"}},
            required=("query",),
        ),
        "output_schema": object_schema(),
        "handler": lambda _args, _ctx: ToolResult(ok=True, summary="ok"),
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
    policy_module: str | None = None,
    policy_builder: str = "build_policy",
) -> ToolPackEntry:
    return ToolPackEntry(
        name=name,
        description=f"Catalog test pack {name}.",
        policy_module=policy_module,
        policy_builder=policy_builder,
        provider_module=module,
    )


def _provider_module(
    module: str,
    packs: dict[str, tuple[ToolDef, ...]],
    *,
    provider_id: str | None = None,
) -> ModuleType:
    return _module(
        module,
        TOOL_PROVIDER=ToolProvider(
            id=provider_id or module.rsplit(".", 1)[-1],
            packs=packs,
            module=module,
        ),
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


def test_audit_enforces_exact_provider_pack_membership() -> None:
    module_name = "chatcopilot.external_tools.tests.membership_tools"
    module = _provider_module(
        module_name,
        {"tests.other": (_tool("declared"), _tool("orphan"))},
    )
    packs = {
        "tests.membership": _pack("tests.membership", module_name)
    }

    report = _audit(packs, {module_name: module})
    codes = {issue.code for issue in report.issues}

    assert "tool_provider.pack_missing" in codes
    assert "tool_provider.pack_unassigned" in codes


def test_shared_module_and_explicit_shared_tool_are_valid_and_loaded_once() -> None:
    module_name = "chatcopilot.external_tools.tests.shared_tools"
    shared = _tool("shared")
    module = _provider_module(
        module_name,
        {
            "tests.first": (_tool("first"), shared),
            "tests.second": (_tool("second"), shared),
        },
    )
    calls: list[str] = []

    def load(module_path: str) -> ModuleType:
        calls.append(module_path)
        return {module_name: module}[module_path]

    report = audit_component_catalog(
        tool_packs={
            "tests.first": _pack("tests.first", module_name),
            "tests.second": _pack("tests.second", module_name),
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
        first_name: _provider_module(
            first_name, {"tests.first": (_tool("same_name"),)}
        ),
        second_name: _provider_module(
            second_name, {"tests.second": (_tool("same_name"),)}
        ),
    }
    report = _audit(
        {
            "tests.first": _pack("tests.first", first_name),
            "tests.second": _pack("tests.second", second_name),
        },
        modules,
    )

    conflicts = [issue for issue in report.issues if issue.code == "tool.name_conflict"]

    assert len(conflicts) == 1
    assert conflicts[0].tool == "same_name"
    assert first_name in conflicts[0].module
    assert second_name in conflicts[0].module


def test_policy_mapping_and_policy_result_type_are_checked() -> None:
    tools_name = "chatcopilot.external_tools.tests.prompt_tools"
    policy_name = "chatcopilot.external_tools.tests.prompt_policy"
    builder = lambda: ("not-a-policy",)  # noqa: E731
    modules = {
        tools_name: _provider_module(
            tools_name, {"tests.prompt": (_tool(),)}
        ),
        policy_name: _module(
            policy_name,
            build_policy=builder,
            TOOL_PACK_POLICY_BUILDERS={},
        ),
    }
    report = _audit(
        {
            "tests.prompt": _pack(
                "tests.prompt",
                tools_name,
                policy_module=policy_name,
            )
        },
        modules,
    )

    assert {issue.code for issue in report.issues} >= {
        "policy.mapping_mismatch",
        "policy.result_invalid",
    }


def test_tool_security_contract_and_schema_are_checked_without_calling_handler() -> None:
    calls: list[object] = []

    def handler(args, _ctx):
        calls.append(args)
        return ToolResult(ok=True, summary="ok")

    module_name = "chatcopilot.external_tools.tests.security_tools"
    tool = _tool(
        handler=handler,
        requires_role="owenr",
        input_schema=object_schema(
            {"query": {"type": "string", "default": object()}},
            required=("query",),
        ),
    )
    module = _provider_module(module_name, {"tests.security": (tool,)})

    report = _audit(
        {"tests.security": _pack("tests.security", module_name)},
        {module_name: module},
    )

    assert calls == []
    assert {issue.code for issue in report.issues} >= {
        "tool.requires_role_invalid",
        "tool.schema_invalid",
    }


def test_old_one_argument_handler_is_rejected_without_execution() -> None:
    calls: list[object] = []

    def old_handler(args):
        calls.append(args)
        return ToolResult(ok=True, data={})

    module_name = "chatcopilot.external_tools.tests.old_handler_tools"
    module = _provider_module(
        module_name,
        {"tests.old-handler": (_tool(handler=old_handler),)},
    )

    report = _audit(
        {"tests.old-handler": _pack("tests.old-handler", module_name)},
        {module_name: module},
    )

    assert calls == []
    assert "tool.handler_signature_invalid" in {
        issue.code for issue in report.issues
    }


def test_malformed_runtime_values_become_issues_instead_of_crashing() -> None:
    module_name = "chatcopilot.external_tools.tests.malformed_tools"
    malformed_tool = _tool(
        requires_role=[],
        execution_policy=[],
        weight=[],
        audiences=("main", "worker"),
        artifact_kinds=([],),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query", []],
            "additionalProperties": False,
        },
    )
    malformed_pack = ToolPackEntry(
        name="tests.malformed",
        description="Malformed runtime values.",
        provider_module=module_name,
    )
    malformed_mcp = McpCatalogEntry(
        id="tests-mcp",
        title="Catalog test MCP",
        source_url="https://example.com/mcp",
        server=[],  # type: ignore[arg-type]
    )

    report = _audit(
        {"tests.malformed": malformed_pack},
        {
            module_name: _provider_module(
                module_name, {"tests.malformed": (malformed_tool,)}
            )
        },
        mcp_entries={"tests-mcp": malformed_mcp},
    )

    assert {issue.code for issue in report.issues} >= {
        "mcp.server_invalid",
        "tool.artifact_kinds_invalid",
        "tool.audiences_invalid",
        "tool.execution_policy_invalid",
        "tool.requires_role_invalid",
        "tool.weight_invalid",
        "tool.input_schema_invalid",
    }


def test_module_namespace_is_fail_closed_and_exception_text_is_redacted() -> None:
    calls: list[str] = []
    hidden = "".join(("secret", "-import-detail"))

    def load(module_path: str) -> ModuleType:
        calls.append(module_path)
        raise RuntimeError(hidden)

    outside_report = audit_component_catalog(
        tool_packs={
            "tests.outside": _pack("tests.outside", "urllib.request")
        },
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
        module_loader=load,
    )
    assert calls == []
    assert {issue.code for issue in outside_report.issues} == {
        "tool_pack.provider_module_invalid"
    }

    allowed_name = "chatcopilot.external_tools.tests.import_failure"
    import_report = audit_component_catalog(
        tool_packs={
            "tests.import": _pack("tests.import", allowed_name)
        },
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
        module_loader=load,
    )
    payload = json.dumps(import_report.to_dict(), ensure_ascii=False)

    assert "tool_provider.import_failed" in payload
    assert "RuntimeError" in payload
    assert hidden not in payload


def test_cross_surface_delegate_name_and_workflow_references_are_checked() -> None:
    module_name = "chatcopilot.external_tools.tests.delegate_tools"
    module = _provider_module(
        module_name,
        {"tests.delegate": (_tool("delegate_demo"),)},
    )
    subagent = SubagentDef(
        name="demo_agent",
        tool_name="delegate_demo",
        summary="Delegate a catalog test.",
        role_prompt="Return a structured result.",
    )
    workflow = WorkflowDef(
        name="demo_flow",
        tool_name="run_demo_flow",
        summary="Run a catalog test workflow.",
        steps=("missing_agent",),
    )

    report = _audit(
        {"tests.delegate": _pack("tests.delegate", module_name)},
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
