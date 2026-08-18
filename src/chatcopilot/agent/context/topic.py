"""Topic relevance routing before the main agent LLM call.

This module is intentionally platform-neutral.  Middleware may pass chat hints
through ``AgentTask.metadata``, but the classifier only decides which message
view the main LLM should receive; it never mutates the canonical transcript.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, cast

from chatcopilot.core.llm_client import ChatResult

_LOGGER = logging.getLogger("chatcopilot.agent.context.topic")

TopicDecisionKind = Literal["related", "unrelated", "uncertain"]
TopicDecisionSource = Literal["rules", "llm", "cache", "fallback", "disabled", "empty_history"]

_POLICY_VERSION = "topic-relevance-v1"

_RELATED_RE = re.compile(
    r"(继续|接着|上面|刚才|上一(?:条|轮|个)|前面|这个|这份|这个文件|这个任务|它|再(?:帮我|处理|看|查|改|跑)|"
    r"按你说的|照这个|job[_-]?[A-Za-z0-9]+)",
    re.IGNORECASE,
)
_UNRELATED_RE = re.compile(
    r"(新话题|换个(?:问题|话题)|重新开始|不要参考上文|别看上文|忽略上文|另一个问题|另外问一下)",
    re.IGNORECASE,
)

_CLASSIFIER_SYSTEM_PROMPT = """You are a topic relevance router for a chat agent.
Decide whether the latest user message needs the immediately previous dialogue to be understood.

Return strict JSON only:
{"decision":"related|unrelated|uncertain","confidence":0.0,"reason":"short reason"}

Definitions:
- related: the latest message depends on, refers to, modifies, or follows up the previous dialogue.
- unrelated: the latest message is independently understandable and about a different task/topic.
- uncertain: not enough evidence.

Be conservative: short pronoun-heavy messages are usually related; complete standalone requests in group chats are often unrelated.
"""


class TopicLlm(Protocol):
    @property
    def model(self) -> str:
        ...

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResult:
        ...


@dataclass(frozen=True)
class TopicPolicy:
    """Runtime policy for topic relevance routing."""

    enabled: bool = False
    mode: Literal["llm", "rules", "off"] = "off"
    model: str | None = None
    uncertain_mode: Literal["continue", "new_topic"] = "continue"
    related_threshold: float = 0.70
    unrelated_threshold: float = 0.75
    current_max_chars: int = 1200
    previous_user_max_chars: int = 800
    previous_assistant_max_chars: int = 800
    decision_cache_size: int = 256
    decision_cache_ttl_seconds: int = 300

    @property
    def active(self) -> bool:
        return self.enabled and self.mode != "off"

    def fallback_context_kind(self) -> TopicDecisionKind:
        return "unrelated" if self.uncertain_mode == "new_topic" else "related"


@dataclass(frozen=True)
class TopicDecision:
    """A topic decision plus the effective context mode for this turn."""

    kind: TopicDecisionKind
    confidence: float
    reason: str
    source: TopicDecisionSource
    context_kind: TopicDecisionKind
    usage: Mapping[str, Any] | None = None
    model: str | None = None

    @classmethod
    def related(cls, *, source: TopicDecisionSource, reason: str, confidence: float = 1.0) -> "TopicDecision":
        return cls(
            kind="related",
            context_kind="related",
            confidence=confidence,
            reason=reason,
            source=source,
        )

    @classmethod
    def unrelated(cls, *, source: TopicDecisionSource, reason: str, confidence: float = 1.0) -> "TopicDecision":
        return cls(
            kind="unrelated",
            context_kind="unrelated",
            confidence=confidence,
            reason=reason,
            source=source,
        )


@dataclass(frozen=True)
class TopicTurn:
    current_user: str
    previous_user: str
    previous_assistant: str


class TopicDecisionCache:
    """Small TTL LRU cache for repeated platform retries."""

    def __init__(self, *, max_size: int, ttl_seconds: int) -> None:
        self._max_size = max(0, max_size)
        self._ttl_seconds = max(0, ttl_seconds)
        self._items: OrderedDict[str, tuple[float, TopicDecision]] = OrderedDict()

    def get(self, key: str) -> TopicDecision | None:
        if self._max_size <= 0 or self._ttl_seconds <= 0:
            return None
        item = self._items.get(key)
        if item is None:
            return None
        created_at, decision = item
        if time.monotonic() - created_at > self._ttl_seconds:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return TopicDecision(
            kind=decision.kind,
            context_kind=decision.context_kind,
            confidence=decision.confidence,
            reason=decision.reason,
            source="cache",
            usage=decision.usage,
            model=decision.model,
        )

    def set(self, key: str, decision: TopicDecision) -> None:
        if self._max_size <= 0 or self._ttl_seconds <= 0:
            return
        self._items[key] = (time.monotonic(), decision)
        self._items.move_to_end(key)
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)


class TopicRelevanceClassifier:
    """Classify whether the current turn should see previous dialogue."""

    def __init__(self, llm: TopicLlm, policy: TopicPolicy) -> None:
        self._llm = llm
        self._policy = policy
        self._cache = TopicDecisionCache(
            max_size=policy.decision_cache_size,
            ttl_seconds=policy.decision_cache_ttl_seconds,
        )

    @property
    def policy(self) -> TopicPolicy:
        return self._policy

    def classify(
        self,
        *,
        messages: list[dict[str, Any]],
        current_user_text: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> TopicDecision:
        if not self._policy.active:
            return TopicDecision.related(source="disabled", reason="topic classifier disabled")

        turn = extract_topic_turn(messages, current_user_text=current_user_text, policy=self._policy)
        if not turn.previous_user and not turn.previous_assistant:
            return TopicDecision.related(source="empty_history", reason="no previous turn")

        rule_decision = self._classify_by_rules(turn, metadata or {})
        if rule_decision is not None:
            return rule_decision
        if self._policy.mode == "rules":
            return self._fallback("rules mode had no strong signal")

        key = self._cache_key(turn, metadata or {})
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        decision = self._classify_by_llm(turn, metadata or {})
        self._cache.set(key, decision)
        return decision

    def _classify_by_rules(
        self,
        turn: TopicTurn,
        metadata: Mapping[str, Any],
    ) -> TopicDecision | None:
        current = turn.current_user.strip()
        if not current:
            return TopicDecision.related(source="rules", reason="empty current message")
        if _UNRELATED_RE.search(current):
            return TopicDecision.unrelated(source="rules", reason="explicit new-topic cue")
        if _RELATED_RE.search(current):
            return TopicDecision.related(source="rules", reason="explicit follow-up cue")
        if _truthy(metadata.get("has_quote")) or _truthy(metadata.get("has_attachment")):
            return TopicDecision.related(source="rules", reason="message carries quote or attachment context")
        if len(current) <= 12 and not _looks_standalone_question(current):
            return TopicDecision.related(source="rules", reason="short context-dependent message", confidence=0.85)
        return None

    def _classify_by_llm(self, turn: TopicTurn, metadata: Mapping[str, Any]) -> TopicDecision:
        payload = {
            "current_user": turn.current_user,
            "previous_user": turn.previous_user,
            "previous_assistant": turn.previous_assistant,
            "metadata": {
                "chat_kind": str(metadata.get("chat_kind") or ""),
                "has_attachment": _truthy(metadata.get("has_attachment")),
                "has_quote": _truthy(metadata.get("has_quote")),
                "message_count": metadata.get("message_count"),
            },
        }
        messages = [
            {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ]
        try:
            result = self._llm.chat(
                messages=messages,
                tools=None,
                stream=False,
                model=self._policy.model or None,
                max_retries=0,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("topic relevance LLM failed: %s", exc)
            return self._fallback(f"classifier failed: {type(exc).__name__}")
        return self._parse_llm_result(result)

    def _parse_llm_result(self, result: ChatResult) -> TopicDecision:
        try:
            payload = json.loads(_extract_json_object(result.content))
        except (json.JSONDecodeError, TypeError, ValueError):
            return self._fallback("classifier returned invalid JSON", usage=result.usage)

        raw_kind = str(payload.get("decision") or "").strip().lower()
        confidence = _coerce_confidence(payload.get("confidence"))
        reason = str(payload.get("reason") or "classifier decision").strip()[:200]
        if raw_kind not in {"related", "unrelated", "uncertain"}:
            return self._fallback("classifier returned unknown decision", usage=result.usage)

        kind = cast(TopicDecisionKind, raw_kind)
        context_kind: TopicDecisionKind = kind
        if kind == "related" and confidence < self._policy.related_threshold:
            kind = "uncertain"
        elif kind == "unrelated" and confidence < self._policy.unrelated_threshold:
            kind = "uncertain"
        if kind == "uncertain":
            context_kind = self._policy.fallback_context_kind()

        return TopicDecision(
            kind=kind,
            context_kind=context_kind,
            confidence=confidence,
            reason=reason,
            source="llm",
            usage=result.usage,
            model=self._policy.model or getattr(self._llm, "model", None),
        )

    def _fallback(self, reason: str, *, usage: Mapping[str, Any] | None = None) -> TopicDecision:
        return TopicDecision(
            kind="uncertain",
            context_kind=self._policy.fallback_context_kind(),
            confidence=0.0,
            reason=reason,
            source="fallback",
            usage=usage,
        )

    def _cache_key(self, turn: TopicTurn, metadata: Mapping[str, Any]) -> str:
        payload = {
            "version": _POLICY_VERSION,
            "current": turn.current_user,
            "previous_user": turn.previous_user,
            "previous_assistant": turn.previous_assistant,
            "chat_kind": str(metadata.get("chat_kind") or ""),
            "has_attachment": _truthy(metadata.get("has_attachment")),
            "has_quote": _truthy(metadata.get("has_quote")),
            "thresholds": [
                self._policy.related_threshold,
                self._policy.unrelated_threshold,
                self._policy.uncertain_mode,
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_topic_turn(
    messages: list[dict[str, Any]],
    *,
    current_user_text: str,
    policy: TopicPolicy,
) -> TopicTurn:
    """Extract only the latest and immediately previous dialogue for routing."""
    current_user = _truncate(current_user_text, policy.current_max_chars)
    previous_user = ""
    previous_assistant = ""
    user_indices = [idx for idx, msg in enumerate(messages) if msg.get("role") == "user"]
    if len(user_indices) >= 2:
        previous_start = user_indices[-2]
        current_start = user_indices[-1]
        previous_messages = messages[previous_start:current_start]
        previous_user = _truncate(
            _first_content(previous_messages, "user"),
            policy.previous_user_max_chars,
        )
        previous_assistant = _truncate(
            _last_content(previous_messages, "assistant"),
            policy.previous_assistant_max_chars,
        )
    return TopicTurn(
        current_user=current_user,
        previous_user=previous_user,
        previous_assistant=previous_assistant,
    )


def _first_content(messages: list[dict[str, Any]], role: str) -> str:
    for msg in messages:
        if msg.get("role") == role:
            return _stringify_content(msg.get("content"))
    return ""


def _last_content(messages: list[dict[str, Any]], role: str) -> str:
    for msg in reversed(messages):
        if msg.get("role") == role:
            return _stringify_content(msg.get("content"))
    return ""


def _stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _truncate(text: str, max_chars: int) -> str:
    max_chars = max(0, max_chars)
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _extract_json_object(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return text[start:end + 1]


def _coerce_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _looks_standalone_question(text: str) -> bool:
    return bool(re.search(r"(什么是|如何|怎么|为什么|请|帮我|生成|分析|查询|列出|解释)", text))


__all__ = [
    "TopicDecision",
    "TopicDecisionCache",
    "TopicPolicy",
    "TopicRelevanceClassifier",
    "TopicTurn",
    "extract_topic_turn",
]
