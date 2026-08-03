"""字符级 token 估算器。

不依赖特定 tokenizer 库，用字符分类粗略估算 token 数。
CJK 字符约 1.2 token/字，ASCII 约 0.25 token/字符，适用于
中英文混合场景的上下文预算控制——只需"大致正确"即可。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence

_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x2E80, 0x2EFF),
    (0x3000, 0x303F),
    (0xFF00, 0xFFEF),
    (0xF900, 0xFAFF),
)

_CJK_FACTOR = 1.2
_ASCII_FACTOR = 0.25
_MSG_OVERHEAD = 4
ESTIMATOR_VERSION = "char-v2-message-system-tools"


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Estimate token count from raw text."""
    if not text:
        return 0
    cjk = 0
    ascii_chars = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            ascii_chars += 1
    return max(1, int(cjk * _CJK_FACTOR + ascii_chars * _ASCII_FACTOR + 0.5))


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """Estimate token count for a single OpenAI-style message.

    Accounts for role/name overhead (~4 tokens) plus content.
    """
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return _MSG_OVERHEAD + estimate_tokens(content)


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """Estimate total token count for a messages list."""
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_tool_schema_tokens(tools: Sequence[Mapping[str, Any]] | None) -> int:
    """Estimate serialized tool-schema tokens included in an LLM request."""
    if not tools:
        return 0
    return estimate_tokens(
        json.dumps(list(tools), ensure_ascii=False, separators=(",", ":"), default=str)
    )


def estimate_prompt_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> Dict[str, int | str]:
    """Return an auditable rough estimate for the full model input."""
    normalized_messages = [dict(message) for message in messages]
    message_tokens = estimate_messages_tokens(normalized_messages)
    system_tokens = sum(
        estimate_message_tokens(message)
        for message in normalized_messages
        if message.get("role") == "system"
    )
    tool_schema_tokens = estimate_tool_schema_tokens(tools)
    return {
        "tokens": message_tokens + tool_schema_tokens,
        "message_tokens": message_tokens,
        "system_tokens": system_tokens,
        "tool_schema_tokens": tool_schema_tokens,
        "estimator_version": ESTIMATOR_VERSION,
    }


__all__ = [
    "ESTIMATOR_VERSION",
    "estimate_tokens",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_tool_schema_tokens",
    "estimate_prompt_tokens",
]
