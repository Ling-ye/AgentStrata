from __future__ import annotations

from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.search_probe import run_probes


def _tool(handler) -> ToolDef:
    return ToolDef(
        name="probe_search",
        summary="Probe search",
        input_schema=object_schema(
            {"query": {"type": "string"}},
            required=("query",),
        ),
        output_schema=object_schema(
            {
                "server_id": {"type": "string"},
                "tool_name": {"type": "string"},
                "content": {},
            },
            required=("server_id", "tool_name", "content"),
        ),
        handler=handler,
        metadata={"mcp_server_id": "probe", "mcp_remote_name": "search"},
    )


def _config() -> McpServerConfig:
    return McpServerConfig(id="probe", search_only_tools=("search",))


def test_run_probes_passes_explicit_context_and_reads_structured_content() -> None:
    def handler(args: dict, ctx: ToolContext) -> ToolResult:
        assert args == {"query": "latest"}
        assert ctx.request_text == "latest"
        return ToolResult(
            ok=True,
            summary="summary is not the result payload",
            data={
                "server_id": "probe",
                "tool_name": "search",
                "content": {"results": [{"title": "current result"}]},
            },
        )

    result = run_probes(
        (_config(),),
        (_tool(handler),),
        query="latest",
        url="https://example.com",
        require_results=True,
    )[0]

    assert result.ok is True
    assert result.status == "passed"
    assert result.result_count == 1
    assert "current result" in result.sample


def test_run_probes_preserves_structured_tool_failure() -> None:
    def handler(_args: dict, _ctx: ToolContext) -> ToolResult:
        return ToolResult(
            ok=False,
            error="remote unavailable",
            error_code="mcp_unavailable",
            stage="remote_call",
        )

    result = run_probes(
        (_config(),),
        (_tool(handler),),
        query="latest",
        url="https://example.com",
        require_results=True,
    )[0]

    assert result.ok is False
    assert result.status == "failed"
    assert result.error_code == "mcp_unavailable"
    assert result.message == "remote unavailable"
