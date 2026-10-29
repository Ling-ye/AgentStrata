"""Runtime/model independence, private transport and explicit migration contracts."""

from tests.application_fixture import write_application

import json
import inspect

import pytest

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.model import ModelSpec
from chatcopilot.contracts.model_runtime import (
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


def test_subscription_selection_ignores_api_key_and_stays_nonsecret(model_settings):
    config = resolve_model_config(
        ModelSpec(binding="bot.lingye-copilot-qq.chat"),
        fallback=LLMConfig(model="gpt-4o-mini", api_key="do-not-use"),
        prefix="MODEL",
        environment={"MODEL_API_KEY": "other-secret"},
    )
    assert config.api_key == ""
    assert config.api == "chatgpt_responses"
    assert config.base_url == "https://chatgpt.com/backend-api/codex"
    assert "other-secret" not in json.dumps(plain(config))
    for runtime in ("langgraph", "codex"):
        assert (
            ResolvedRuntimeRoute(runtime, config.model_route()).model.auth.mode
            == "chatgpt"
        )


def test_key_mode_uses_explicit_environment_reference(model_settings):
    from tests.model_settings_fixture import write_models
    data = json.loads(model_settings.read_text())
    data["connections"]["responses"]["auth"]["key_env"] = "TEST_KEY"
    write_models(model_settings, data)
    config = resolve_model_config(
        ModelSpec(binding="responses-main"),
        fallback=LLMConfig(model="gpt-4o-mini", ),
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
    reference = RuntimeSessionRef("langgraph", "session")
    assert reference.runtime_id == "langgraph"
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


def test_retired_runtime_cannot_execute_but_archived_binding_preserves_identity():
    with pytest.raises(ValueError, match="unknown Agent runtime"):
        ResolvedRuntimeRoute("native", LLMConfig(model="fixture-model").model_route())
    historical = RuntimeSessionBinding(
        binding_id="old", actor_key="actor", runtime_id="native", native_thread_id="old-thread",
        auth_identity_epoch=1, scope_digest="scope", capability_digest="capability",
        runtime_config_digest="config", status="archived",
    ).to_payload()
    assert RuntimeSessionBinding.from_payload(historical).runtime_id == "native"
    with pytest.raises(ValueError, match="invalid runtime binding"):
        RuntimeSessionBinding.from_payload({**historical, "status": "active"})


def test_codex_resume_does_not_reuse_other_runtime_binding(tmp_path):
    from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter

    path = tmp_path / "session.json"
    binding = RuntimeSessionBinding(
        binding_id="binding", actor_key="actor", runtime_id="langgraph", native_thread_id="other-thread",
        auth_identity_epoch=1, scope_digest="scope", capability_digest="capability",
        runtime_config_digest="config", resume_policy="restartable",
    )
    path.write_text(json.dumps(binding.to_payload()))
    # Loading a resume binding does not need a model or an app-server process.
    from chatcopilot.core.config import ChatConfig
    from tests.prompt_plan_fixture import runtime_route

    config = ChatConfig(llm=LLMConfig(provider="openai", api="openai_responses", model="gpt-test", api_key="fixture"))
    adapter = CodexRuntimeAdapter(tool_names=set(), route=runtime_route("codex", config.llm), runtime_config=config)
    assert adapter._load_native_session_state(path, acp_session_id="actor", policy_fingerprint="scope") == ("", 0)
    assert json.loads(path.read_text())["runtime_id"] == "langgraph"


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
        "create_model_client",
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
    config = LLMConfig(model="gpt-4o-mini", provider="openai", api="openai_responses", api_key="secret")
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
        responses_chat(LLMConfig(model="gpt-4o-mini", provider="openai", api="openai_responses", api_key="key"), [], [])
    assert len(calls) == 1


@pytest.mark.parametrize("error_type,category", [
    ("ConnectTimeout", "connect_timeout"), ("ReadTimeout", "read_timeout"),
    ("ProxyError", "proxy_error"), ("SSLError", "tls_error"),
    ("ConnectionError", "connection_error"),
])
def test_responses_connection_diagnostics_are_typed_and_redacted(monkeypatch, error_type, category):
    import requests

    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise getattr(requests.exceptions, error_type)("fixture private transport detail")

    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", fail)
    with pytest.raises(RuntimeError) as caught:
        responses_chat(LLMConfig(model="fixture", provider="openai", api="openai_responses", api_key="fixture"), [], [])
    attempts = 3 if category == "connect_timeout" else 1
    suffix = "connection attempts exhausted" if attempts > 1 else "request was not replayed"
    assert str(caught.value) == f"Responses connection failed ({category}); {suffix}"
    assert "fixture private transport detail" not in str(caught.value)
    assert len(calls) == attempts


def _handshake_eof():
    import ssl
    import requests

    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    connection = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname="fixture.invalid")
    incoming.write_eof()
    with pytest.raises(ssl.SSLEOFError) as caught:
        connection.do_handshake()
    return requests.exceptions.SSLError(caught.value)


def test_initial_tls_handshake_eof_reconnects_before_a_single_completed_response(monkeypatch):
    calls = []
    response = Response([{"type": "response.completed", "response": {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "READY"}]},
    ]}}])

    def post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise _handshake_eof()
        return response

    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", post)
    result = responses_chat(LLMConfig(model="fixture", api="openai_responses", api_key="fixture"), [], [])
    assert result.content == "READY" and len(calls) == 3 and response.closed
    assert calls[0]["timeout"] > calls[1]["timeout"] > calls[2]["timeout"]


def test_tls_certificate_failure_and_eof_without_handshake_proof_are_not_retried(monkeypatch):
    import requests
    import ssl

    for cause in (ssl.SSLCertVerificationError(1, "fixture certificate"), ssl.SSLEOFError(8, "fixture EOF")):
        calls = []

        def post(*args, **kwargs):
            calls.append(kwargs)
            raise requests.exceptions.SSLError(cause)

        monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", post)
        with pytest.raises(RuntimeError, match="request was not replayed"):
            responses_chat(LLMConfig(model="fixture", api="openai_responses", api_key="fixture"), [], [])
        assert len(calls) == 1


def test_tls_error_after_stream_output_never_replays_the_response(monkeypatch):
    calls, deltas = [], []

    class Disconnected(Response):
        def iter_lines(self):
            yield from super().iter_lines()
            raise _handshake_eof()

    response = Disconnected([{"type": "response.output_text.delta", "delta": "partial"}])
    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post",
                        lambda *args, **kwargs: (calls.append(kwargs), response)[1])
    with pytest.raises(RuntimeError, match="stream failed.*not replayed"):
        responses_chat(LLMConfig(model="fixture", api="openai_responses", api_key="fixture"), [], [],
                       on_content_delta=deltas.append)
    assert len(calls) == 1 and deltas == ["partial"] and response.closed


def test_connection_retries_keep_the_original_deadline_and_honor_cancellation(monkeypatch):
    import requests
    from types import SimpleNamespace

    calls, checks = [], []

    def post(*args, **kwargs):
        calls.append(kwargs)
        raise _handshake_eof()

    def cancel():
        checks.append(True)
        if len(calls):
            raise RuntimeError("fixture cancelled")

    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", post)
    with pytest.raises(RuntimeError, match="fixture cancelled"):
        responses_chat(LLMConfig(model="fixture", api="openai_responses", api_key="fixture"), [], [],
                       cancellation=SimpleNamespace(raise_if_cancelled=cancel))
    assert len(calls) == 1

    clock = [0.0]
    monkeypatch.setattr("chatcopilot.core.responses_client.time.monotonic", lambda: clock[0])

    def expired(*args, **kwargs):
        clock[0] = 10.0
        raise requests.ConnectTimeout("fixture connection timeout")

    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post", expired)
    with pytest.raises(RuntimeError, match="connection budget exhausted"):
        responses_chat(LLMConfig(model="fixture", api="openai_responses", api_key="fixture"), [], [], timeout=1)


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
            LLMConfig(model="gpt-4o-mini", provider="openai", api="openai_responses", api_key="fixture"), [], []
        )


def test_old_backend_field_fails_instead_of_silently_switching(tmp_path):
    path = tmp_path / "bot.yaml"
    write_application(path, "id: test\nagents:\n  backend: codex\n")
    with pytest.raises(ValueError, match="agents contains unsupported field"):
        load_botspec(path)


def test_langgraph_responses_loop_executes_tools_and_keeps_continuation_private(monkeypatch):
    from chatcopilot.agent.langgraph_session import LangGraphAgentSession
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
        "subprocess.Popen", lambda *a, **k: pytest.fail("LangGraph must not start Codex")
    )
    tool = ToolDef(
        "lookup",
        "Lookup",
        {"type": "object", "properties": {}},
        {"type": "object"},
        lambda a, c: (executions.append(1), ToolResult(ok=True, summary="record"))[1],
        access="member",
    )
    session = LangGraphAgentSession(
        session_id="langgraph",
        llm=LLMClient(LLMConfig(model="gpt-4o-mini", provider="openai", api="openai_responses", api_key="private-key")),
        executor=ToolExecutor(tools=[tool], caller_role_hint="user"),
        tools_schema=[build_openai_schema(tool)],
        prompt_plan=prompt_plan("host"),
        resolved_runtime_id="langgraph",
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
        } == {"langgraph"}
        assert "private-continuation" not in repr(snapshots)
        assert "private-key" not in repr(snapshots)
    finally:
        session.close()
