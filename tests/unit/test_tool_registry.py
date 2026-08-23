from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import ModuleType

import pytest

from chatcopilot.agent.tools.registry import (
    ToolMaterializationError,
    ToolRegistry,
)
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema


def _tool(name: str = "demo_tool") -> ToolDef:
    def handler(arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary="ok", data={"value": arguments.get("value", "")})

    return ToolDef(
        name=name,
        summary="Registry contract test tool.",
        input_schema=object_schema(
            {"value": {"type": "string"}},
            required=("value",),
        ),
        output_schema=object_schema(
            {"value": {"type": "string"}},
            required=("value",),
        ),
        handler=handler,
        category="tests.registry",
        owner="tests",
        module=__name__,
        artifact_kinds=(),
    )


def _provider(
    provider_id: str,
    pack_id: str,
    *tools: ToolDef,
    module: str | None = None,
) -> ToolProvider:
    return ToolProvider(
        id=provider_id,
        packs={pack_id: tuple(tools)},
        module=module or f"chatcopilot.tests.{provider_id}",
    )


def test_runtime_provider_builds_one_snapshot_for_openai_mcp_and_execution() -> None:
    registry = ToolRegistry()
    registry.register_runtime_provider(_provider("runtime", "runtime.tools", _tool()))

    snapshot = registry.snapshot(
        tool_packs=("runtime.tools",),
        require_all_selected=True,
    )

    assert tuple(snapshot.index) == ("demo_tool",)
    assert [entry["function"]["name"] for entry in snapshot.openai_schema] == [
        "demo_tool"
    ]
    assert [entry["name"] for entry in snapshot.mcp_schema] == ["demo_tool"]
    assert snapshot.mcp_schema[0]["outputSchema"] == snapshot.index["demo_tool"].output_schema
    with pytest.raises(FrozenInstanceError):
        snapshot.tools = ()  # type: ignore[misc]


def test_describe_reports_provider_pack_and_handler_source() -> None:
    registry = ToolRegistry((_provider("runtime", "runtime.tools", _tool()),))

    source = registry.describe("demo_tool", tool_packs=("runtime.tools",))

    assert source is not None
    assert source.provider_id == "runtime"
    assert source.pack_id == "runtime.tools"
    assert source.provider_module == "chatcopilot.tests.runtime"
    assert source.handler_module == __name__
    assert source.handler_symbol.endswith("_tool.<locals>.handler")


def test_catalog_loads_only_the_explicit_selected_provider_module() -> None:
    module = ModuleType("chatcopilot.agent.tools.builtin.memory_tools")
    module.TOOL_PROVIDER = _provider(
        "memory",
        "memory.chat",
        _tool("read_memory"),
        module=module.__name__,
    )
    calls: list[str] = []

    def load(module_path: str) -> ModuleType:
        calls.append(module_path)
        assert module_path == module.__name__
        return module

    registry = ToolRegistry.from_catalog(("memory.chat",), module_loader=load)
    snapshot = registry.snapshot(tool_packs=("memory.chat",))

    assert calls == [module.__name__]
    assert tuple(snapshot.index) == ("read_memory",)


def test_known_dynamic_pack_can_wait_for_session_provider() -> None:
    registry = ToolRegistry.from_catalog(("persona.control",))

    assert registry.snapshot(tool_packs=("persona.control",)).tools == ()
    with pytest.raises(ToolMaterializationError) as caught:
        registry.snapshot(
            tool_packs=("persona.control",),
            require_all_selected=True,
        )
    assert caught.value.reason == "provider_not_registered"


def test_registration_rejects_duplicate_provider_pack_and_tool_names() -> None:
    registry = ToolRegistry((_provider("one", "one.tools", _tool("one")),))

    with pytest.raises(ToolMaterializationError, match="duplicate_provider_id"):
        registry.register_provider(_provider("one", "other.tools", _tool("other")))
    with pytest.raises(ToolMaterializationError, match="duplicate_pack_provider"):
        registry.register_provider(_provider("two", "one.tools", _tool("two")))

    registry.register_provider(_provider("two", "two.tools", _tool("one")))
    with pytest.raises(ToolMaterializationError) as caught:
        registry.snapshot(tool_packs=("one.tools", "two.tools"))
    assert caught.value.reason == "duplicate_tool_name"
    assert caught.value.tool_names == ("one",)


def test_same_provider_can_share_one_tool_across_packs_without_duplication() -> None:
    shared = _tool("shared")
    provider = ToolProvider(
        id="shared-provider",
        packs={"first.tools": (shared,), "second.tools": (shared,)},
        module="chatcopilot.tests.shared_provider",
    )
    registry = ToolRegistry((provider,))

    snapshot = registry.snapshot(tool_packs=("first.tools", "second.tools"))

    assert tuple(snapshot.index) == ("shared",)
    assert snapshot.describe("shared").pack_id == "first.tools"  # type: ignore[union-attr]


def test_registration_rejects_one_argument_handler() -> None:
    tool = _tool()

    def one_argument_handler(_arguments: dict) -> ToolResult:
        return ToolResult(ok=True, data={"value": ""})

    tool.handler = one_argument_handler  # type: ignore[assignment]

    with pytest.raises(ToolMaterializationError) as caught:
        ToolRegistry((_provider("invalid", "invalid.tools", tool),))

    assert caught.value.reason == "invalid_tool_handler_signature"
    assert caught.value.tool_names == ("demo_tool",)


@pytest.mark.parametrize("schema_field", ["input_schema", "output_schema"])
def test_registration_rejects_invalid_json_schema(schema_field: str) -> None:
    tool = _tool()
    setattr(tool, schema_field, {"type": "not-a-json-schema-type"})

    with pytest.raises(ToolMaterializationError) as caught:
        ToolRegistry((_provider("invalid", "invalid.tools", tool),))

    assert caught.value.reason.startswith(f"invalid_{schema_field.removesuffix('_schema')}_schema")
    assert caught.value.tool_names == ("demo_tool",)


def test_unknown_pack_and_missing_static_provider_fail_closed() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolMaterializationError, match="unknown_tool_pack"):
        registry.snapshot(tool_packs=("missing.pack",))

    with pytest.raises(ToolMaterializationError) as caught:
        registry.snapshot(tool_packs=("memory.chat",))
    assert caught.value.reason == "provider_not_registered"
