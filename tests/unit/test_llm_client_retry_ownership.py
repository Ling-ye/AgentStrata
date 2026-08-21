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
