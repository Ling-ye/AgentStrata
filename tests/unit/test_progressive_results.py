from __future__ import annotations

import json
from dataclasses import replace

import pytest

from chatcopilot.agent.backends.session_relay import SessionToolRelay, call_session_relay
from chatcopilot.agent.context.manager import _summarize_tool_message
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.tools.result_reader import (
    PAGE_CHARS,
    SessionResultStore,
    result_reader_provider,
)
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema


def source_tool(body, *, access="member", calls=None):
    def handler(_args, _ctx):
        if calls is not None:
            calls.append(True)
        return ToolResult(
            ok=True, summary="Document retrieved", data={"content": body, "source": "fixture"}
        )

    return ToolDef(
        name="fixture_read",
        summary="Read a fixture",
        input_schema=object_schema({}),
        output_schema=object_schema({"content": {}, "source": {"type": "string"}}),
        handler=handler,
        access=access,
        metadata={"result_content_field": "content"},
    )


def test_long_result_survives_history_compression_and_unicode_pagination():
    body = "开头" + "材料" * 6000 + "末尾证据"
    tool = source_tool(body)
    store = SessionResultStore()
    original = tool.handler({}, ToolContext()).to_llm_payload()
    projected = store.project(tool, original)
    assert original["data"]["content"] == body
    assert "content" not in projected["data"]
    assert projected["data"]["source"] == "fixture"
    assert projected["truncated"] is True
    ref = projected["result_ref"]
    message = {"role": "tool", "content": json.dumps(projected, ensure_ascii=False)}
    compressed = json.loads(_summarize_tool_message(message, 10)["content"])
    assert compressed["result_ref"] == ref
    offset, parts = 0, []
    while offset is not None:
        page = store.read({"result_id": ref["id"], "offset": offset}, ToolContext())
        assert page.ok and page.data["sha256"] == ref["sha256"]
        parts.append(page.data["text"])
        offset = page.data["next_offset"]
    assert "".join(parts) == body
    tail = store.read({"result_id": ref["id"], "query": "末尾证据"}, ToolContext())
    assert tail.data["text"] == "末尾证据"
    assert tail.data["next_offset"] is None


def test_short_structured_snapshot_is_immutable_and_available_after_compression():
    body = {"results": [{"name": "original", "value": 42}]}
    tool, store = source_tool(body), SessionResultStore()
    projected = store.project(tool, tool.handler({}, ToolContext()).to_llm_payload())
    ref = projected["result_ref"]
    assert projected["data"]["content"] == body
    body["results"][0]["value"] = 99
    page = store.read({"result_id": ref["id"]}, ToolContext())
    assert json.loads(page.data["text"])["results"][0]["value"] == 42


def test_readback_rechecks_original_permission_and_cannot_cross_sessions():
    allowed = True
    store = SessionResultStore(lambda _tool: None if allowed else "permission revoked")
    tool = source_tool("private", access="owner")
    ref = store.project(tool, tool.handler({}, ToolContext()).to_llm_payload())["result_ref"]["id"]
    assert (
        SessionResultStore().read({"result_id": ref}, ToolContext()).error_code
        == "tool_result_unavailable"
    )
    allowed = False
    denied = store.read({"result_id": ref}, ToolContext(caller_role="owner"))
    assert denied.error_code == "tool_permission_denied"
    unfiltered_store = SessionResultStore()
    ref = unfiltered_store.project(tool, tool.handler({}, ToolContext()).to_llm_payload())[
        "result_ref"
    ]["id"]
    assert (
        unfiltered_store.read({"result_id": ref}, ToolContext()).error_code
        == "tool_permission_denied"
    )


def test_lru_expiration_close_and_oversized_results_are_explicit():
    tool, store = source_tool("x" * 300), SessionResultStore(max_bytes=800)
    payload = tool.handler({}, ToolContext()).to_llm_payload()
    first = store.project(tool, payload)["result_ref"]["id"]
    second = store.project(tool, payload)["result_ref"]["id"]
    assert store.read({"result_id": first}, ToolContext()).error_code == "tool_result_unavailable"
    assert store.read({"result_id": second}, ToolContext()).ok
    store.close()
    assert store.read({"result_id": second}, ToolContext()).error_code == "tool_result_unavailable"
    projected = SessionResultStore(max_bytes=10).project(tool, payload)
    assert projected["data"] == payload["data"]
    assert projected["result_cache"] == "not_cached_oversized" and "result_ref" not in projected
    message = {"role": "tool", "content": json.dumps(projected)}
    assert _summarize_tool_message(message, 1) == message


def test_errors_unmarked_results_and_receipts_remain_complete():
    tool, store = source_tool("x" * (PAGE_CHARS + 1)), SessionResultStore()
    error = ToolResult(
        ok=False, error="failed", error_code="remote_failed", data={"content": "details"}
    ).to_llm_payload()
    assert store.project(tool, error) == error
    payload = tool.handler({}, ToolContext()).to_llm_payload()
    assert store.project(replace(tool, metadata={}), payload) == payload
    receipt_tool = replace(
        tool, metadata={"result_content_field": "content", "result_inline_only": True}
    )
    receipt = store.project(receipt_tool, payload)
    assert receipt["data"] == payload["data"]
    message = {"role": "tool", "content": json.dumps(receipt)}
    assert _summarize_tool_message(message, 1) == message


@pytest.mark.parametrize(
    "args", [{"offset": -1}, {"offset": 999}, {"limit": PAGE_CHARS + 1}, {"limit": 0}]
)
def test_read_ranges_fail_without_silent_truncation(args):
    tool, store = source_tool("value"), SessionResultStore()
    ref = store.project(tool, tool.handler({}, ToolContext()).to_llm_payload())["result_ref"]["id"]
    assert (
        store.read({"result_id": ref, **args}, ToolContext()).error_code
        == "tool_result_range_invalid"
    )


def test_codex_relay_filters_before_caching_and_does_not_reexecute():
    calls = []
    tool = source_tool("start sensitive-value " + "x" * PAGE_CHARS + " tail-marker", calls=calls)
    store = SessionResultStore()
    reader = result_reader_provider(store).packs["context.results"][0]
    tools = [tool, reader]
    executor = ToolExecutor(tools=tools, caller_role_hint="user", result_store=store)

    def filtered(payload):
        return json.loads(json.dumps(payload).replace("sensitive-value", "[redacted]"))

    relay = SessionToolRelay(tools=tools, executor=executor, payload_filter=filtered)
    endpoint = relay.start().to_dict()
    try:
        response = call_session_relay(
            endpoint, {"action": "call_tool", "name": tool.name, "arguments": {}}
        )
        result = response["result"]
        assert result["truncated"] is True and "sensitive-value" not in json.dumps(result)
        ref = result["result_ref"]["id"]
        read = call_session_relay(
            endpoint, {"action": "call_tool", "name": reader.name, "arguments": {"result_id": ref}}
        )
        assert "[redacted]" in read["result"]["data"]["text"]
        assert "sensitive-value" not in json.dumps(read)
        tail = store.read({"result_id": ref, "query": "tail-marker"}, ToolContext())
        assert tail.data["text"] == "tail-marker" and len(calls) == 1
    finally:
        relay.close()
        executor.close()


@pytest.mark.parametrize("backend_id", ["native", "langgraph"])
def test_inprocess_backends_share_projection_and_clear_on_close(backend_id):
    from unittest.mock import Mock

    from chatcopilot.agent.backends.registry import build_backend
    from chatcopilot.contracts.agent import AgentTask
    from chatcopilot.contracts.agent_backend import BackendOpenRequest
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.llm_client import ChatResult
    from tests.prompt_plan_fixture import prompt_plan

    calls = []
    tool, store = source_tool("first " + "中" * 9000 + " last", calls=calls), SessionResultStore()
    reader = result_reader_provider(store).packs["context.results"][0]
    executor = ToolExecutor(tools=[tool, reader], caller_role_hint="user", result_store=store)
    model = Mock(model="fixture")
    model.chat.side_effect = [
        ChatResult(
            content="",
            tool_calls=[
                {
                    "id": "read-1",
                    "type": "function",
                    "function": {"name": tool.name, "arguments": "{}"},
                }
            ],
        ),
        ChatResult(content="done"),
    ]
    backend = build_backend(
        backend_id,
        tool_names={tool.name, reader.name},
        llm=model,
        runtime_config=ChatConfig(),
        tool_executor=executor,
        tools_schema=[],
    )
    session_ref = backend.open_session(
        BackendOpenRequest(session_id="test", prompt_plan=prompt_plan("fixture"))
    )
    try:
        result = backend.stream_turn(
            session_ref, AgentTask("read the document"), on_event=lambda _: None
        )
        assert result.final_text == "done" and len(calls) == 1
        messages = backend.native_session(session_ref).snapshot_messages()
        payload = json.loads(next(msg["content"] for msg in messages if msg.get("role") == "tool"))
        ref = payload["result_ref"]["id"]
        assert store.read({"result_id": ref, "query": "last"}, ToolContext()).data["text"] == "last"
    finally:
        backend.close_session(session_ref)
    assert store.read({"result_id": ref}, ToolContext()).error_code == "tool_result_unavailable"


def test_runtime_registers_reader_only_with_accessible_sources_and_isolates_actors():
    from unittest.mock import Mock

    from chatcopilot.agent.runtime import AgentRuntime
    from chatcopilot.core.config import ChatConfig
    from tests.prompt_plan_fixture import prompt_input

    tool = source_tool("actor result", access="owner")
    runtime = AgentRuntime(
        llm=Mock(model="fixture"), tools=(tool,), tools_schema=(), runtime_config=ChatConfig()
    )
    owner = runtime.new_session(
        session_id="owner-a", prompt_input=prompt_input("fixture", role="owner")
    )
    other = runtime.new_session(
        session_id="owner-b", prompt_input=prompt_input("fixture", role="owner")
    )
    member = runtime.new_session(
        session_id="member", prompt_input=prompt_input("fixture", role="user")
    )
    try:
        first = owner.backend.native_session(owner.backend_session_ref)
        second = other.backend.native_session(other.backend_session_ref)
        restricted = member.backend.native_session(member.backend_session_ref)
        assert "read_tool_result" in {item["function"]["name"] for item in first.tools_schema}
        assert "read_tool_result" not in {
            item["function"]["name"] for item in restricted.tools_schema
        }
        result = first.executor.execute(tool.name, {})
        projected = first.executor.project_result(tool.name, result.to_llm_payload())
        ref = projected["result_ref"]["id"]
        assert first.executor.execute("read_tool_result", {"result_id": ref}).ok
        assert (
            second.executor.execute("read_tool_result", {"result_id": ref}).error_code
            == "tool_result_unavailable"
        )
    finally:
        owner.close()
        other.close()
        member.close()


def test_without_reader_keeps_body_during_subagent_compaction():
    from chatcopilot.agent.context.manager import ContextManager

    body = "evidence " * 1200 + "tail fact"
    tool = source_tool(body)
    executor = ToolExecutor(tools=[tool], caller_role_hint="user")
    payload = executor.project_result(tool.name, executor.execute(tool.name, {}).to_llm_payload())
    assert payload["result_inline_required"] is True and "result_ref" not in payload
    messages = [
        {"role": "user", "content": "check the evidence"},
        {"role": "assistant", "tool_calls": [{"id": "first"}]},
        {"role": "tool", "tool_call_id": "first", "content": json.dumps(payload)},
        {"role": "assistant", "tool_calls": [{"id": "second"}]},
        {"role": "tool", "tool_call_id": "second", "content": '{"summary":"latest"}'},
    ]
    context = ContextManager(
        max_context_tokens=100000,
        tool_result_summary_max_tokens=1,
        summarize_prior_tool_results=True,
    )
    prepared = context.prepare_messages(messages, prompt_prefix_length=0)
    assert json.loads(prepared[2]["content"])["data"]["content"] == body
