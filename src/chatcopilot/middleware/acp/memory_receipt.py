"""Receipt requirements for explicit conversation-memory mutations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from chatcopilot.core.memory_intent import classify_memory_intent
from chatcopilot.core.memory_policy import evaluate_memory_content


@dataclass(frozen=True)
class MemoryReceiptRequirement:
    kind: str
    successful_tools: frozenset[str]
    scope: str
    retry_context: str
    failure_text: str
    retry_allowed: bool = True

    def matches(self, event: Any) -> bool:
        """Match a host mutation receipt, never just the tool name."""
        if not event.ok or event.name not in self.successful_tools:
            return False
        outer = event.data
        if not isinstance(outer, Mapping):
            return False
        data = outer.get("data")
        if not isinstance(data, Mapping) or data.get("scope") != self.scope:
            return False
        if data.get("request_bound") is not True:
            return False
        if self.kind == "memory_clear":
            return data.get("action") == "clear" and data.get("cleared") is True
        if self.kind == "memory_delete":
            return (
                data.get("action") == "delete"
                and isinstance(data.get("item_id"), str)
                and bool(data["item_id"])
                and type(data.get("version")) is int
                and data["version"] > 0
                and isinstance(data.get("content_sha256"), str)
                and len(data["content_sha256"]) == 64
            )
        if self.kind == "memory_append":
            return (
                data.get("action") == "append"
                and isinstance(data.get("item_id"), str)
                and bool(data["item_id"])
                and type(data.get("version")) is int
                and data["version"] > 0
                and isinstance(data.get("content_sha256"), str)
                and len(data["content_sha256"]) == 64
            )
        return False


def classify_memory_receipt_requirement(
    user_text: str,
    *,
    caller_role: str,
    is_group: bool = False,
) -> MemoryReceiptRequirement | None:
    """Classify explicit requests without promoting a partial forget to a wipe."""
    intent = classify_memory_intent(user_text)
    scope = "group" if is_group else "user"
    owner = (caller_role or "").strip().lower() == "owner"
    if intent == "clear":
        return MemoryReceiptRequirement(
            kind="memory_clear",
            successful_tools=frozenset({"clear_memory"}),
            scope=scope,
            retry_context=(
                "用户明确要求清空当前作用域全部记忆。只有 clear_memory "
                "返回与当前作用域绑定的成功回执后才能声称已清空。"
            ) if owner else "",
            failure_text=(
                "未能清空记忆：只有 Owner 可以执行整份清空。"
                if not owner else
                "未能完成记忆清空：本轮没有匹配的清空回执。"
            ),
            retry_allowed=owner,
        )
    if intent == "targeted_forget":
        return MemoryReceiptRequirement(
            kind="memory_delete",
            successful_tools=frozenset({"manage_memory"}),
            scope=scope,
            retry_context=(
                "这是单条忘记请求。先定位唯一条目，再由 Owner 调用 manage_memory "
                "删除；目标不唯一时列出候选，不得清空整份记忆。"
            ) if owner else "",
            failure_text=(
                "未能删除这条记忆：只有 Owner 可以执行删除操作。"
                if not owner else
                "未能删除这条记忆：没有匹配的单条删除回执；目标不唯一时需要先定位。"
            ),
            retry_allowed=owner,
        )
    if intent == "append":
        decision = evaluate_memory_content(user_text, scope=scope)
        if not decision.allowed:
            return MemoryReceiptRequirement(
                kind="memory_rejected",
                successful_tools=frozenset(),
                scope=scope,
                retry_context="",
                failure_text=f"未保存这条记忆：{decision.reason}",
                retry_allowed=False,
            )
        return MemoryReceiptRequirement(
            kind="memory_append",
            successful_tools=frozenset({"append_memory"}),
            scope=scope,
            retry_context=(
                "用户明确要求记住信息。请从当前原始请求中选取要保存的原文片段，"
                "调用 append_memory；只有内容与请求绑定的成功回执才能说已保存。"
            ),
            failure_text="未能保存这条记忆：本轮没有与当前请求内容匹配的写入回执。",
        )
    return None


__all__ = ["MemoryReceiptRequirement", "classify_memory_receipt_requirement"]
