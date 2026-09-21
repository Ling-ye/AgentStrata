"""Runtime/model independence, private transport and explicit migration contracts."""

import json
import inspect

import pytest

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.model import ModelSpec
from chatcopilot.contracts.model_runtime import (
    ApiKeyAuthRef,
    ChatGPTAuthRef,
    ModelToolCall,
    ResolvedModelRoute,
    ResolvedRuntimeRoute,
    parse_auth,
)
from chatcopilot.contracts.execution import RuntimeSessionBinding
from chatcopilot.contracts.runtime_adapter import RuntimeSessionRef
from chatcopilot.agent.runtime import build_agent_runtime
from chatcopilot.core.config import LLMConfig
from chatcopilot.core.inspection import plain
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.core.model_routes import resolve_model_config
from chatcopilot.core.responses_client import responses_chat


def test_subscription_selection_ignores_api_key_and_stays_nonsecret():
    config = resolve_model_config(
        ModelSpec(provider="openai", model="test-model", auth=ChatGPTAuthRef()),
        fallback=LLMConfig(api_key="do-not-use"),
        prefix="MODEL",
        environment={"MODEL_API_KEY": "other-secret"},
    )
    assert config.api_key == ""
    assert config.api == "chatgpt_responses"
    assert config.base_url == "https://chatgpt.com/backend-api/codex"
    assert "other-secret" not in json.dumps(plain(config))
    for runtime in ("native", "codex", "langgraph"):
        assert (
            ResolvedRuntimeRoute(runtime, config.model_route()).model.auth.mode
            == "chatgpt"
        )


def test_key_mode_uses_explicit_environment_reference():
    config = resolve_model_config(
        ModelSpec(provider="openai", auth=ApiKeyAuthRef("TEST_KEY")),
        fallback=LLMConfig(),
        prefix="TEST",
        environment={"TEST_KEY": "private-value"},
    )
    assert config.api == "openai_responses" and config.api_key == "private-value"
    assert "private-value" not in repr(config)
    assert "private-value" not in json.dumps(plain(config))


def test_runtime_route_is_the_only_agent_runtime_factory_selector() -> None:
    parameters = inspect.signature(build_agent_runtime).parameters
    assert "route" in parameters
    assert "runtime_id" not in parameters
    assert "agent_backend" not in parameters


def test_runtime_session_contracts_have_no_backend_aliases() -> None:
    reference = RuntimeSessionRef("native", "session")
    assert reference.runtime_id == "native"
    assert not hasattr(reference, "backend")
    binding = RuntimeSessionBinding(
        binding_id="binding",
        actor_key="actor",
        runtime_id="codex",
        native_thread_id="thread",
        auth_identity_epoch=1,
        scope_digest="scope",
        capability_digest="capability",
        runtime_config_digest="runtime",
    )
    assert binding.to_payload()["schema_version"] == 4
    with pytest.raises(ValueError, match="explicit migration"):
        RuntimeSessionBinding.from_payload({**binding.to_payload(), "schema_version": 3})


@pytest.mark.parametrize(
    "auth",
    [
        {"mode": "chatgpt", "profile": "main", "key_env": "KEY"},
        {"mode": "api_key", "key_env": "KEY", "token": "secret"},
    ],
)
def test_mixed_or_secret_bearing_auth_is_rejected(auth):
    with pytest.raises(ValueError):
        parse_auth(auth)


def test_subscription_cannot_be_sent_to_custom_endpoint():
    with pytest.raises(ValueError):
        ResolvedModelRoute(
            "openai", "model", "chatgpt_responses", "https://fixture.invalid", ChatGPTAuthRef()
        )


def test_nested_tool_arguments_are_frozen():
    original = {"items": [{"name": "first"}]}
    call = ModelToolCall("id", "read", original)
    original["items"][0]["name"] = "changed"
    assert call.arguments["items"][0]["name"] == "first"
    with pytest.raises(TypeError):
        call.arguments["items"][0]["name"] = "changed"


def test_codex_does_not_create_a_host_main_model_client_at_assembly(monkeypatch):
    from chatcopilot.agent import runtime as runtime_module
    from chatcopilot.core.deferred_model import DeferredModelClient

    config = LLMConfig(
        provider="openai",
        api="openai_responses",
        model="gpt-test",
        api_key="fixture",
    )
    monkeypatch.setattr(
        runtime_module,
        "LLMClient",
        lambda _config: pytest.fail("Codex assembly constructed a host main-model client"),
    )
    runtime = build_agent_runtime(
        chat_config=runtime_module.ChatConfig(llm=config),
        route=ResolvedRuntimeRoute("codex", config.model_route(), 900),
        tool_packs=(),
    )
    try:
        assert runtime.main_model_client is None
        assert isinstance(runtime.subagent_default_model_client, DeferredModelClient)
    finally:
        runtime.close()


class Response:
    status_code = 200
    closed = False

    def __init__(self, events):
        self.events = events

    def iter_lines(self):
        for event in self.events:
            yield ("data: " + json.dumps(event)).encode()
            yield b""

    def close(self):
        self.closed = True


def test_responses_returns_calls_only_after_terminal_response(monkeypatch):
    sent = []
    response = Response(
        [
            {"type": "response.function_call_arguments.delta", "delta": "{bad partial"},
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "read",
                            "arguments": '{"path":"a"}',
                        }
                    ],
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            },
        ]
    )
    monkeypatch.setattr(
        "chatcopilot.core.responses_client.requests.post",
        lambda *a, **kw: (sent.append(kw), response)[1],
    )
    config = LLMConfig(provider="openai", api="openai_responses", api_key="secret")
    result = responses_chat(config, [{"role": "user", "content": "read"}], [])
    assert result.tool_calls[0]["id"] == "c1"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"path": "a"}
    assert sent[0]["json"]["store"] is False and response.closed
    assert result.usage["prompt_tokens"] == 4


def test_stream_disconnection_is_not_replayed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "chatcopilot.core.responses_client.requests.post",
        lambda *a, **kw: (
            calls.append(kw),
            Response([{"type": "response.output_text.delta", "delta": "partial"}]),
        )[1],
    )
    with pytest.raises(RuntimeError, match="not replayed"):
        responses_chat(LLMConfig(provider="openai", api="openai_responses", api_key="key"), [], [])
    assert len(calls) == 1


def test_duplicate_response_tool_call_ids_fail_before_tool_execution(monkeypatch):
    response = Response(
        [
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "duplicate",
                            "name": "save",
                            "arguments": "{}",
                        },
                        {
                            "type": "function_call",
                            "call_id": "duplicate",
                            "name": "save",
                            "arguments": "{}",
                        },
                    ]
                },
            }
        ]
    )
    monkeypatch.setattr(
        "chatcopilot.core.responses_client.requests.post", lambda *a, **kw: response
    )
    with pytest.raises(ValueError, match="duplicate tool call"):
        responses_chat(
            LLMConfig(provider="openai", api="openai_responses", api_key="fixture"), [], []
        )


def test_old_backend_field_fails_instead_of_silently_switching(tmp_path):
    path = tmp_path / "bot.yaml"
    path.write_text("id: test\nagents:\n  backend: codex\n")
    with pytest.raises(ValueError, match="agents contains unsupported field"):
        load_botspec(path)


def test_native_responses_loop_executes_tools_and_keeps_continuation_private(monkeypatch):
    from chatcopilot.agent.session import AgentSession
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.contracts.agent import AgentTask, ContextSnapshotPrepared
    from chatcopilot.contracts.tools import ToolDef, ToolResult, build_openai_schema
    from tests.prompt_plan_fixture import prompt_plan

    executions, requests, events = [], [], []
    responses = iter(
        [
            Response(
                [
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [
                                {
                                    "type": "reasoning",
                                    "id": "r1",
                                    "encrypted_content": "private-continuation",
                                },
                                {
                                    "type": "function_call",
                                    "call_id": "c1",
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            ]
                        },
                    }
                ]
            ),
            Response(
                [
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": "done"}],
                                }
                            ]
                        },
                    }
                ]
            ),
        ]
    )

    def post(*args, **kwargs):
        requests.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", post)
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: pytest.fail("Native must not start Codex")
    )
    tool = ToolDef(
        "lookup",
        "Lookup",
        {"type": "object", "properties": {}},
        {"type": "object"},
        lambda a, c: (executions.append(1), ToolResult(ok=True, summary="record"))[1],
        access="member",
    )
    session = AgentSession(
        session_id="native",
        llm=LLMClient(LLMConfig(provider="openai", api="openai_responses", api_key="private-key")),
        executor=ToolExecutor(tools=[tool], caller_role_hint="user"),
        tools_schema=[build_openai_schema(tool)],
        prompt_plan=prompt_plan("host"),
        resolved_runtime_id="native",
    )
    try:
        result = session.run_task(AgentTask("lookup"), on_event=events.append)
        assert result.stop_reason == "end_turn" and result.final_text == "done"
        assert executions == [1] and len(requests) == 2
        assert any(
            item.get("call_id") == "c1" and item.get("type") == "function_call_output"
            for item in requests[1]["input"]
        )
        assert "private-continuation" in repr(requests[1])
        snapshots = [event for event in events if isinstance(event, ContextSnapshotPrepared)]
        assert len(snapshots) == 2 and all(
            event.context_kind == "responses_request" for event in snapshots
        )
        assert {
            event.runtime_id for event in events if hasattr(event, "runtime_id")
        } == {"native"}
        assert "private-continuation" not in repr(snapshots)
        assert "private-key" not in repr(snapshots)
    finally:
        session.close()
