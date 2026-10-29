"""Independent discovery and failure-budget regression for the owned Agent."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.mcp.tool_wrapper import _wrap_remote_tool
from chatcopilot.agent.tools.disclosure import ToolDisclosureView
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.tools.registry import ToolRegistry
from chatcopilot.contracts.agent import AgentTask, ToolFinished
from chatcopilot.contracts.chat_result import ChatResult
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.evals.evaluation_runtime import permission_filter
from integrations.open_llm_vtuber.agentstrata_vtuber.config import AdapterConfig, Motion
from integrations.open_llm_vtuber.agentstrata_vtuber.control import install_control
from chatcopilot.protocols.gateway_client import GatewayClientConfig
from tests.prompt_plan_fixture import prompt_plan


@pytest.fixture
def avatar_view(monkeypatch):
    """Read the actual adapter's FastMCP descriptors, then use host wrapping/Registry."""
    from fastapi import FastAPI
    from mcp.server import fastmcp

    real_fastmcp = fastmcp.FastMCP
    captured = []

    def capture(*args, **kwargs):
        server = real_fastmcp(*args, **kwargs)
        captured.append(server)
        return server

    monkeypatch.setattr(fastmcp, "FastMCP", capture)
    config = AdapterConfig(
        GatewayClientConfig("ws://127.0.0.1:1", "g" * 48, client_id="fixture"),
        "m" * 48, "fixture-avatar", {"happy": 0},
        {"greet": Motion("TapBody", 0)}, ("http://localhost:12393",),
    )
    controller = SimpleNamespace(list_actions=lambda: {})
    install_control(FastAPI(), config, controller)
    assert len(captured) == 1
    remote = asyncio.run(captured[0].list_tools())
    mcp_config = McpServerConfig(
        id="fixture-avatar", transport="streamable_http",
        url="http://127.0.0.1:12393/agentstrata/mcp/",
        exposure="main", risk="interactive", tool_prefix="avatar",
        allowed_tools=("list_actions", "set_expression", "play_motion", "apply_pose"),
    )
    wrapped = tuple(
        _wrap_remote_tool(mcp_config, SimpleNamespace(), tool, "avatar" + tool.name)
        for tool in remote
    )
    registry = ToolRegistry()
    registry.register_runtime_provider(ToolProvider(
        id="mcp.dynamic", packs={"mcp.dynamic": wrapped},
        module="chatcopilot.agent.mcp.client",
    ))
    snapshot = registry.snapshot(tool_packs=("mcp.dynamic",))
    def view(role="owner", allowed=None):
        policy = permission_filter(
            frozenset(allowed if allowed is not None else (tool.name for tool in wrapped)),
            role=role,
        )
        visible = tuple(tool for tool in snapshot.tools if policy(tool) is None)
        return ToolDisclosureView(
            visible, {tool.name: snapshot.sources[tool.name].pack_id for tool in visible},
        )
    return view


@pytest.mark.parametrize("query", [
    "expression motion", "motion expression", "set expression play motion",
])
def test_real_adapter_combination_is_top_hit(avatar_view, query):
    view = avatar_view()
    found = view.search({"queries": [query], "limit": 1})
    assert found.ok
    assert [item["name"] for item in found.data["results"][0]["matches"]] == [
        "avatarapply_pose",
    ]
    described = view.describe({"names": ["avatarapply_pose"]})
    assert {"expression", "motion"} <= set(
        described.data["tools"]["avatarapply_pose"]["input_schema"]["properties"]
    )


def test_pack_alias_chinese_authorization_and_stable_order(avatar_view):
    view = avatar_view()
    assert view.search({"queries": ["mcp.dynamic"], "limit": 10}).data["results"][0]["matches"]
    assert view.search({"queries": ["avatarapply_pose"], "limit": 1}).data["results"][0]["matches"][0]["name"] == "avatarapply_pose"
    first = view.search({"queries": ["motion"], "limit": 10}).data
    assert first == view.search({"queries": ["motion"], "limit": 10}).data
    restricted = avatar_view(role="user")
    assert restricted.search({"queries": ["expression motion"]}).data["results"][0]["matches"] == []
    assert restricted.describe({"names": ["avatarapply_pose"]}).data["tools"] == {}
    assert not restricted.resolve_call({"name": "avatarapply_pose", "arguments": {}}).ok
    alias = ToolDef(
        name="status_report", summary="查询状态",
        input_schema=object_schema({}), output_schema=object_schema({}),
        handler=lambda _args, _ctx: ToolResult(ok=True), aliases=["state_alias"],
    )
    local = ToolDisclosureView((alias,), {"status_report": "report.pack"})
    for query in ("state_alias", "查询状态", "report.pack"):
        assert local.search({"queries": [query], "limit": 1}).data["results"][0]["matches"][0]["name"] == "status_report"


def test_three_concepts_require_full_coverage_before_partial_schema_match():
    def item(name, fields):
        return ToolDef(
            name=name, summary="Report readings",
            input_schema=object_schema({field: {"type": "number"} for field in fields}),
            output_schema=object_schema({}), handler=lambda _a, _c: ToolResult(ok=True),
        )
    view = ToolDisclosureView((
        item("partial_report", ("temperature", "humidity")),
        item("complete_report", ("temperature", "humidity", "pressure")),
    ), {"partial_report": "weather", "complete_report": "weather"})
    found = view.search({"queries": ["temperature humidity pressure"], "limit": 1})
    assert [entry["name"] for entry in found.data["results"][0]["matches"]] == [
        "complete_report",
    ]


def _call(name, args, call_id):
    return ChatResult(tool_calls=[{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }], finish_reason="tool_calls")


class _Model:
    model = "synthetic-model"
    config = SimpleNamespace(api="chat_completions")

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _run_sequence(responses, handler, *, max_calls=None):
    tool = ToolDef(
        name="business", summary="Process business request",
        input_schema=object_schema({"key": {"type": "string"}}, required=("key",)),
        output_schema=object_schema({}), handler=handler, access="member",
    )
    view = ToolDisclosureView((tool,), {"business": "business.pack"})
    model = _Model(responses)
    session = LangGraphAgentSession(
        session_id="failure-budget", llm=model,
        executor=ToolExecutor(tools=[tool], caller_role_hint="user"),
        tools_schema=view.model_schemas(), disclosure=view,
        prompt_plan=prompt_plan(), resolved_runtime_id="langgraph",
        max_tool_calls=max_calls, max_consecutive_tool_failures=2,
        stream_first_turn=False,
    )
    events = []
    try:
        result = session.run_task(AgentTask("Process the requested business operation."),
                                  on_event=events.append)
        return result, model, events
    finally:
        session.close()


@pytest.mark.parametrize("catalog_name,catalog_args", [
    ("tool_search", {"queries": ["business"]}),
    ("tool_describe", {"names": ["business"]}),
])
def test_catalog_success_preserves_business_failure_limit(catalog_name, catalog_args):
    attempts = []
    def unavailable(args, _context):
        attempts.append(args["key"])
        return ToolResult(ok=False, error="service unavailable", error_code="unavailable")
    result, model, events = _run_sequence([
        _call("tool_call", {"name": "business", "arguments": {"key": "first"}}, "a"),
        _call(catalog_name, catalog_args, "b"),
        _call("tool_call", {"name": "business", "arguments": {"key": "second"}}, "c"),
        ChatResult(content="unexpected continuation", finish_reason="stop"),
    ], unavailable)
    assert result.stop_reason == "tool_failure_cap"
    assert len(model.calls) == 3
    assert attempts == ["first", "second"]
    assert [(e.name, e.ok) for e in events if isinstance(e, ToolFinished)] == [
        ("business", False), (catalog_name, True), ("business", False),
    ]


def test_real_business_success_resets_failure_limit():
    attempts = []
    def execute(args, _context):
        key = args["key"]
        attempts.append(key)
        return ToolResult(ok=key == "recovered", summary="done" if key == "recovered" else "",
                          error=None if key == "recovered" else "unavailable")
    result, model, _events = _run_sequence([
        _call("tool_call", {"name": "business", "arguments": {"key": "first"}}, "a"),
        _call("tool_describe", {"names": ["business"]}, "b"),
        _call("tool_call", {"name": "business", "arguments": {"key": "recovered"}}, "c"),
        _call("tool_call", {"name": "business", "arguments": {"key": "later"}}, "d"),
        ChatResult(content="done", finish_reason="stop"),
    ], execute)
    assert result.stop_reason == "end_turn"
    assert attempts == ["first", "recovered", "later"]
    assert len(model.calls) == 5


def test_catalog_calls_do_not_spend_business_call_budget():
    attempts = []
    def execute(args, _context):
        attempts.append(args["key"])
        return ToolResult(ok=True, summary="done")
    result, model, events = _run_sequence([
        _call("tool_search", {"queries": ["business"]}, "a"),
        _call("tool_describe", {"names": ["business"]}, "b"),
        _call("tool_call", {"name": "business", "arguments": {"key": "one"}}, "c"),
        ChatResult(content="done", finish_reason="stop"),
    ], execute, max_calls=1)
    assert result.stop_reason == "end_turn"
    assert attempts == ["one"]
    assert len(model.calls) == 4
    assert [(e.name, e.ok) for e in events if isinstance(e, ToolFinished)] == [
        ("tool_search", True), ("tool_describe", True), ("business", True),
    ]
