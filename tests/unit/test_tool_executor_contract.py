from __future__ import annotations

from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema


def _tool(handler) -> ToolDef:
    return ToolDef(
        name="structured_demo",
        summary="Exercise the structured executor contract.",
        input_schema=object_schema(
            {"value": {"type": "integer"}},
            required=("value",),
        ),
        output_schema=object_schema(
            {"doubled": {"type": "integer"}},
            required=("doubled",),
        ),
        handler=handler,
        category="tests.tools",
        owner="tests",
        module=__name__,
        artifact_kinds=(),
    )


def test_executor_validates_input_before_handler_and_binds_request_text() -> None:
    calls: list[tuple[dict, str]] = []

    def handler(arguments: dict, context: ToolContext) -> ToolResult:
        calls.append((arguments, context.request_text))
        return ToolResult(
            ok=True,
            summary="done",
            data={"doubled": arguments["value"] * 2},
        )

    executor = ToolExecutor(tools=[_tool(handler)])
    rejected = executor.execute("structured_demo", {"value": "2"})

    assert rejected.ok is False
    assert rejected.error_code == "tool_input_schema_invalid"
    assert calls == []

    accepted = executor.execute(
        "structured_demo",
        {"value": 2},
        request_text="原始用户文本",
    )

    assert accepted.ok is True
    assert accepted.data == {"doubled": 4}
    assert accepted.to_llm_payload()["data"] == {"doubled": 4}
    assert calls == [({"value": 2}, "原始用户文本")]


def test_executor_rejects_success_data_that_breaks_output_schema() -> None:
    def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary="bad", data={"doubled": "four"})

    result = ToolExecutor(tools=[_tool(handler)]).execute(
        "structured_demo",
        {"value": 2},
    )

    assert result.ok is False
    assert result.error_code == "tool_output_schema_invalid"
    assert result.stage == "output_validation"


def test_executor_returns_structured_dispatch_permission_and_handler_errors() -> None:
    unknown = ToolExecutor(tools=[]).execute("missing", {})
    assert unknown.error_code == "tool_not_found"
    assert unknown.stage == "dispatch"

    def handler(_arguments: dict, _context: ToolContext):
        return ("legacy", [], None)

    executor = ToolExecutor(
        tools=[_tool(handler)],
        permission_filter=lambda _tool_def: "denied",
    )
    denied = executor.execute("structured_demo", {"value": 2})
    assert denied.error_code == "tool_permission_denied"
    assert denied.stage == "permission"

    executor = ToolExecutor(tools=[_tool(handler)])
    invalid = executor.execute("structured_demo", {"value": 2})
    assert invalid.error_code == "tool_handler_exception"
    assert invalid.stage == "handler"


def test_executor_rejects_non_json_success_data() -> None:
    def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={"value": object()})

    tool = ToolDef(
        name="non_json_demo",
        summary="Return a non-JSON object.",
        input_schema=object_schema(),
        output_schema=object_schema(
            {"value": {}},
            required=("value",),
        ),
        handler=handler,
        category="tests.tools",
        owner="tests",
        module=__name__,
        artifact_kinds=(),
    )

    result = ToolExecutor(tools=[tool]).execute("non_json_demo", {})

    assert result.ok is False
    assert result.error_code == "tool_output_json_invalid"
    assert result.stage == "output_validation"
