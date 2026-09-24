"""Authorized tool discovery and execution through the main-runtime bridge."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.tools.disclosure import ToolDisclosureView
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.agent import AgentTask, ContextSnapshotPrepared, ToolCatalogObserved, ToolFinished, ToolStarted
from chatcopilot.contracts.chat_result import ChatResult
from chatcopilot.contracts.tool_validation import validate_tool_contract
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from tests.prompt_plan_fixture import prompt_plan


def _tool(name: str, *, direct: bool = False, access: str = "member", handler=None):
    return ToolDef(
        name=name,
        summary=f"Look up {name} records",
        input_schema=object_schema({"key": {"type": "string"}}, required=("key",)),
        output_schema=object_schema({"value": {"type": "string"}}),
        handler=handler or (lambda _args, _ctx: ToolResult(ok=True, summary="ok")),
        access=access,
        disclosure="direct" if direct else "deferred",
        category="fixture",
    )


def _call(name: str, arguments: dict, call_id: str) -> ChatResult:
    return ChatResult(tool_calls=[{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }], finish_reason="tool_calls")


class _ScriptedModel:
    model = "fixture-model"

    def __init__(self, api: str):
        self.config = SimpleNamespace(api=api)
        self.calls = []
        self.responses = [
            _call("tool_search", {"queries": ["business"]}, "s"),
            _call("tool_describe", {"names": ["business"]}, "d"),
            _call("tool_call", {"name": "business", "arguments": {"key": "x"}}, "c"),
            ChatResult(content="done", finish_reason="stop"),
        ]

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if callback := kwargs.get("on_request_prepared"):
            callback({"tools": kwargs["tools"]})
        return self.responses.pop(0)


def test_catalog_contains_only_authorized_deferred_tools_and_is_stable():
    public = _tool("business")
    direct = _tool("read_memory", direct=True)
    owner = _tool("private_admin", access="owner")
    view = ToolDisclosureView((public, direct), {"business": "business.pack", "read_memory": "memory.chat"})
    names = {item["function"]["name"] for item in view.model_schemas()}
    assert names == {"read_memory", "tool_search", "tool_describe", "tool_call"}
    assert "private_admin" not in json.dumps(view.model_schemas())
    found = view.search({"queries": ["business"]})
    assert found.data["results"][0]["matches"][0]["name"] == "business"
    assert view.search({"queries": ["no matching record"]}).data["available_packs"] == ["business.pack"]
    described = view.describe({"names": ["business", "private_admin"]}).data
    assert list(described["tools"]) == ["business"]
    assert "private_admin" not in json.dumps(described["tools"])
    assert isinstance(view.resolve_call({"name": "private_admin", "arguments": {}}), ToolResult)
    assert isinstance(view.resolve_call({"name": "read_memory", "arguments": {}}), ToolResult)
    assert owner.name not in view.deferred
    assert view.model_schemas() == view.model_schemas()


def test_bridge_names_are_rejected_at_tool_materialization():
    violations = validate_tool_contract(_tool("tool_call"), require_provenance=False)
    assert any(item.materialization_reason == "reserved_tool_name" for item in violations)


@pytest.mark.parametrize("runtime_id,session_type", [
    ("native", AgentSession), ("langgraph", LangGraphAgentSession),
])
@pytest.mark.parametrize("api", ["chat_completions", "openai_responses"])
def test_main_runtime_discovers_and_executes_once(runtime_id, session_type, api):
    seen = []

    def business(args, context):
        seen.append((args, context.request_text))
        return ToolResult(ok=True, summary="business done", data={"value": args["key"]})

    deferred = _tool("business", handler=business)
    direct = _tool("read_memory", direct=True)
    view = ToolDisclosureView((deferred, direct), {"business": "business.pack", "read_memory": "memory.chat"})
    model = _ScriptedModel(api)
    session = session_type(
        session_id="disclosure", llm=model, executor=ToolExecutor(tools=[deferred, direct], caller_role_hint="user"),
        tools_schema=view.model_schemas(), disclosure=view,
        prompt_plan=prompt_plan(runtime_id=runtime_id), resolved_runtime_id=runtime_id,
        max_tool_calls=1, stream_first_turn=False,
    )
    events = []
    result = session.run_task(AgentTask("the exact user request"), on_event=events.append)
    assert result.final_text == "done"
    assert seen == [({"key": "x"}, "the exact user request")]
    assert len(model.calls) == 4
    assert all(call["tools"] == view.model_schemas() for call in model.calls)
    catalogs = [item for item in events if isinstance(item, ToolCatalogObserved)]
    assert len(catalogs) == 1 and set(catalogs[0].tools) == {"business", "read_memory"}
    names = [item.name for item in events if isinstance(item, ToolStarted)]
    assert names == ["tool_search", "tool_describe", "business"]
    finished = [item for item in events if isinstance(item, ToolFinished)]
    assert [item.name for item in finished] == names
    assert finished[-1].tool_call_id == "c"
    assert finished[-1].model_result["name"] == "tool_call"
    assert json.loads(finished[-1].model_result["content"])["data"] == {"value": "x"}
    snapshots = [item for item in events if isinstance(item, ContextSnapshotPrepared)]
    assert snapshots
    assert {item["function"]["name"] for item in snapshots[0].tool_schemas} == {
        "read_memory", "tool_search", "tool_describe", "tool_call",
    }
    session.close()


def test_deferred_call_rechecks_permission_and_input_schema():
    seen = []
    tool = _tool("business", handler=lambda args, _ctx: (seen.append(args), ToolResult(ok=True))[1])
    view = ToolDisclosureView((tool,), {"business": "business.pack"})
    selected = view.resolve_call({"name": "business", "arguments": {}})
    assert selected == ("business", {})
    denied = ToolExecutor(tools=[tool], permission_filter=lambda _tool: "denied").execute(*selected)
    assert denied.error_code == "tool_permission_denied"
    invalid = ToolExecutor(tools=[tool], caller_role_hint="user").execute(*selected)
    assert invalid.error_code == "tool_input_schema_invalid"
    assert seen == []


def test_bridge_rejects_guessed_tool_without_executing_it():
    seen = []
    tool = _tool("business", handler=lambda args, _ctx: (seen.append(args), ToolResult(ok=True))[1])
    view = ToolDisclosureView((tool,), {"business": "business.pack"})
    model = _ScriptedModel("chat_completions")
    model.responses = [
        _call("tool_call", {"name": "owner_secret", "arguments": {}}, "guess"),
        ChatResult(content="unavailable", finish_reason="stop"),
    ]
    session = AgentSession(
        session_id="guess", llm=model,
        executor=ToolExecutor(tools=[tool], caller_role_hint="user"),
        tools_schema=view.model_schemas(), disclosure=view,
        prompt_plan=prompt_plan(), resolved_runtime_id="native", stream_first_turn=False,
    )
    events = []
    session.run_task(AgentTask("request"), on_event=events.append)
    finished = [item for item in events if isinstance(item, ToolFinished)]
    assert len(finished) == 1
    assert finished[0].data["error_code"] == "tool_not_found"
    assert "owner_secret" not in json.dumps(finished[0].data)
    assert seen == []
    session.close()


def test_codex_uses_the_same_tool_disclosure_flags():
    from chatcopilot.agent.runtimes.dynamic_tools import DynamicToolBridge

    direct, deferred = _tool("read_memory", direct=True), _tool("business")
    bridge = DynamicToolBridge(
        tools=(direct, deferred),
        executor=ToolExecutor(tools=[direct, deferred], caller_role_hint="user"),
    )
    schema = bridge.schemas()[0]
    by_name = {item["name"]: item for item in schema["tools"]}
    assert by_name["read_memory"]["deferLoading"] is False
    assert by_name["business"]["deferLoading"] is True
    assert "fixture" in schema["description"]
    bridge.close()


def test_chinese_query_matches_description_without_full_phrase():
    tool = _tool("read_text_head")
    tool.summary = "读取当前工作区文本文件开头"
    view = ToolDisclosureView((tool,), {tool.name: "workspace.read_write"})
    matches = view.search({"queries": ["查工作区文件"]}).data["results"][0]["matches"]
    assert matches[0]["name"] == "read_text_head"


def test_responses_transport_serializes_only_direct_and_bridge_tools(monkeypatch):
    from chatcopilot.core.config import LLMConfig
    from chatcopilot.core.responses_client import responses_chat

    view = ToolDisclosureView((_tool("business"), _tool("read_memory", direct=True)),
                              {"business": "business.pack", "read_memory": "memory.chat"})
    captured = {}

    class RejectedResponse:
        status_code = 503

        def close(self):
            pass

    def post(_url, **kwargs):
        captured.update(kwargs["json"])
        return RejectedResponse()

    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", post)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        responses_chat(LLMConfig(provider="openai", api="openai_responses", api_key="fixture"),
                       [{"role": "user", "content": "lookup"}], view.model_schemas())
    assert {tool["name"] for tool in captured["tools"]} == {
        "read_memory", "tool_search", "tool_describe", "tool_call",
    }
