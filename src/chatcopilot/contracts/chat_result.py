"""Single model-call result, independent of SDK and transport implementations."""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ChatResult:
    """一次 LLM 调用的最终结果。"""

    content: str = ""
    reasoning_content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, int] | None = None
    provider_continuation: tuple[dict, ...] = field(
        default=(), repr=False, metadata={"private": True}
    )

    def to_message(self) -> Dict[str, Any]:
        """转成 OpenAI messages 数组里的 assistant 消息。

        DeepSeek V4 thinking mode 要求在含 tool_calls 的轮次里把
        reasoning_content 原样回传，否则 API 返回 400。
        """
        msg: Dict[str, Any] = {"role": "assistant"}
        if self.content:
            msg["content"] = self.content
        else:
            msg["content"] = None
        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.provider_continuation:
            msg["_provider_continuation"] = self.provider_continuation
        return msg
