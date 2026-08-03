from __future__ import annotations

import json

from chatcopilot.agent.context.topic import (
    TopicPolicy,
    TopicRelevanceClassifier,
    extract_topic_turn,
)
from chatcopilot.core.llm_client import ChatResult


class _FakeLLM:
    model = "fake-main"

    def __init__(self, results: list[ChatResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "分析这个 csv"},
        {"role": "assistant", "content": "我会分析 csv。"},
        {"role": "user", "content": "北京明天天气怎么样？"},
    ]


def test_extract_topic_turn_only_uses_previous_turn() -> None:
    policy = TopicPolicy(enabled=True, mode="llm")

    turn = extract_topic_turn(_messages(), current_user_text="北京明天天气怎么样？", policy=policy)

    assert turn.current_user == "北京明天天气怎么样？"
    assert turn.previous_user == "分析这个 csv"
    assert turn.previous_assistant == "我会分析 csv。"


def test_explicit_related_rule_skips_llm() -> None:
    llm = _FakeLLM([])
    classifier = TopicRelevanceClassifier(llm, TopicPolicy(enabled=True, mode="llm"))
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "分析 a.csv"},
        {"role": "assistant", "content": "分析完成"},
        {"role": "user", "content": "继续看这个文件"},
    ]

    decision = classifier.classify(messages=messages, current_user_text="继续看这个文件")

    assert decision.kind == "related"
    assert decision.source == "rules"
    assert llm.calls == []


def test_llm_unrelated_decision_uses_threshold() -> None:
    llm = _FakeLLM([
        ChatResult(
            content=json.dumps(
                {"decision": "unrelated", "confidence": 0.91, "reason": "standalone weather question"},
                ensure_ascii=False,
            ),
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        )
    ])
    classifier = TopicRelevanceClassifier(llm, TopicPolicy(enabled=True, mode="llm"))

    decision = classifier.classify(
        messages=_messages(),
        current_user_text="北京明天天气怎么样？",
        metadata={"chat_kind": "group"},
    )

    assert decision.kind == "unrelated"
    assert decision.context_kind == "unrelated"
    assert decision.source == "llm"
    assert decision.usage == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    assert llm.calls[0]["stream"] is False
    assert llm.calls[0]["tools"] is None


def test_llm_low_confidence_falls_back_to_policy() -> None:
    llm = _FakeLLM([
        ChatResult(content='{"decision":"unrelated","confidence":0.2,"reason":"weak"}')
    ])
    policy = TopicPolicy(enabled=True, mode="llm", uncertain_mode="continue")
    classifier = TopicRelevanceClassifier(llm, policy)

    decision = classifier.classify(messages=_messages(), current_user_text="北京明天天气怎么样？")

    assert decision.kind == "uncertain"
    assert decision.context_kind == "related"


def test_decision_cache_avoids_repeated_llm_call() -> None:
    llm = _FakeLLM([
        ChatResult(content='{"decision":"unrelated","confidence":0.9,"reason":"standalone"}')
    ])
    classifier = TopicRelevanceClassifier(
        llm,
        TopicPolicy(enabled=True, mode="llm", decision_cache_size=4, decision_cache_ttl_seconds=60),
    )

    first = classifier.classify(messages=_messages(), current_user_text="北京明天天气怎么样？")
    second = classifier.classify(messages=_messages(), current_user_text="北京明天天气怎么样？")

    assert first.source == "llm"
    assert second.source == "cache"
    assert len(llm.calls) == 1
