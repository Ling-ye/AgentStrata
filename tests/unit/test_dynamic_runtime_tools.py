import json
from pathlib import Path
import jsonschema
from chatcopilot.agent.runtimes.dynamic_tools import DynamicToolBridge
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.tools import ToolDef, ToolResult


def test_dynamic_tool_uses_executor_and_exact_current_request():
    seen = []
    tool = ToolDef(
        "business",
        "Business",
        {"type": "object", "properties": {}},
        {"type": "object"},
        lambda args, context: (
            seen.append(context.request_text),
            ToolResult(ok=True, summary="done"),
        )[1],
        access="member",
        audiences=("main",),
    )
    bridge = DynamicToolBridge(
        tools=(tool,), executor=ToolExecutor(tools=[tool], caller_role_hint="user")
    )
    generation = bridge.begin_turn(
        trace_id="trace", parent_span_id="span", depth=1, request_text="user request"
    )
    params = {"namespace": "agentstrata", "tool": "business", "arguments": {}, "threadId": "parent"}
    assert not bridge.call({**params, "threadId": "child"}, main_thread_id="parent")["success"]
    result = bridge.call(params, main_thread_id="parent", generation=generation)
    assert result["success"] and json.loads(result["contentItems"][0]["text"])["ok"]
    assert seen == ["user request"]
    events = bridge.drain_tool_events(generation=generation)
    assert [event["type"] for event in events] == ["agent_event", "tool_started", "tool_finished"]
    bridge.end_turn(generation)
    next_generation = bridge.begin_turn(trace_id="next", parent_span_id="span", depth=1)
    assert next_generation != generation
    assert not bridge.call(params, main_thread_id="parent", generation=generation)["success"]
    bridge.close()


def test_payloads_match_schema_exported_by_pinned_local_codex():
    source = Path(__file__).parents[1] / "fixtures/codex-app-server-0.147.0-alpha.1.2.json"
    fixture = json.loads(source.read_text())
    schemas = fixture["schemas"]
    tool = ToolDef(
        "lookup",
        "Lookup",
        {"type": "object", "properties": {}},
        {"type": "object"},
        lambda a, c: ToolResult(ok=True, summary="ok"),
        access="member",
    )
    bridge = DynamicToolBridge(
        tools=(tool,), executor=ToolExecutor(tools=[tool], caller_role_hint="user")
    )
    jsonschema.validate(
        {"dynamicTools": bridge.schemas(), "approvalPolicy": "on-request"},
        schemas["ThreadStartParams"],
    )
    from chatcopilot.core.model_credentials import AccessCredential

    handoff = AccessCredential("private", "account", 1).handoff()
    jsonschema.validate(handoff, schemas["LoginAccountParams"])
    jsonschema.validate(
        {key: value for key, value in handoff.items() if key != "type"},
        schemas["ChatgptAuthTokensRefreshResponse"],
    )
    jsonschema.validate(bridge._response({"ok": True}), schemas["DynamicToolCallResponse"])
    bridge.close()


def test_repeated_call_id_never_reexecutes_mutation():
    calls = []
    tool = ToolDef(
        "save",
        "Save",
        {"type": "object", "properties": {}},
        {"type": "object"},
        lambda a, c: (calls.append(1), ToolResult(ok=True, summary="saved"))[1],
        access="member",
    )
    bridge = DynamicToolBridge(
        tools=(tool,), executor=ToolExecutor(tools=[tool], caller_role_hint="user")
    )
    bridge.begin_turn(trace_id="trace", parent_span_id="span", depth=1)
    params = {
        "namespace": "agentstrata",
        "tool": "save",
        "arguments": {},
        "threadId": "main",
        "callId": "one",
    }
    assert bridge.call(params, main_thread_id="main")["success"]
    assert not bridge.call(params, main_thread_id="main")["success"]
    assert calls == [1]
    bridge.close()
