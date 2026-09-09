from __future__ import annotations

from types import SimpleNamespace

from chatcopilot.core.config import LLMConfig
from chatcopilot.core.llm_client import LLMClient


def test_openai_sdk_retries_are_disabled_and_owned_by_agentstrata(monkeypatch) -> None:
    captured = {}

    class _OpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("openai.OpenAI", _OpenAI)
    LLMClient(
        LLMConfig(
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="test-model",
            timeout=17,
        )
    )

    assert captured["max_retries"] == 0
    assert captured["timeout"] == 17


def test_reasoning_effort_is_scoped_to_each_streaming_or_blocking_request(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")], usage=None)

    monkeypatch.setattr(LLMClient, "_build_client", lambda self: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    client = LLMClient(LLMConfig(api_key="fixture", model="fixture-model"))
    for stream in (False, True):
        client.chat([{"role": "user", "content": "check"}], stream=stream, reasoning_effort="medium")
        assert calls[-1]["reasoning_effort"] == "medium"
        client.chat([{"role": "user", "content": "check"}], stream=stream)
        assert "reasoning_effort" not in calls[-1]


def test_stream_fallback_preserves_explicit_reasoning_effort(monkeypatch):
    from chatcopilot.core.llm_client import _StreamUnsupported

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")], usage=None)

    def unsupported(*args, **kwargs):
        assert kwargs["reasoning_effort"] == "medium"
        raise _StreamUnsupported()

    monkeypatch.setattr(LLMClient, "_build_client", lambda self: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    client = LLMClient(LLMConfig(api_key="fixture", model="fixture-model"))
    monkeypatch.setattr(client, "_chat_stream", unsupported)
    client.chat([{"role": "user", "content": "check"}], reasoning_effort="medium")
    assert calls[0]["reasoning_effort"] == "medium"
