"""Bounded receipt requirements for explicit conversation-memory mutations."""
from __future__ import annotations

import re
from dataclasses import dataclass

from chatcopilot.core.memory_policy import evaluate_memory_content


_MEMORY_EXPLICIT_RE = re.compile(
    r"(?:请)?记住|记下来|写入记忆|保存(?:下来|为偏好|到记忆)|以后按(?:这个|此|这条)",
    re.IGNORECASE,
)
_MEMORY_CLEAR_RE = re.compile(r"(?:清空|删除|重置|忘掉).{0,12}(?:记忆|长期记忆)", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryReceiptRequirement:
    kind: str
    successful_tools: frozenset[str]
    retry_context: str
    failure_text: str
    retry_allowed: bool = True


def classify_memory_receipt_requirement(
    user_text: str,
    *,
    caller_role: str,
    is_group: bool = False,
) -> MemoryReceiptRequirement | None:
    """Classify explicit memory mutations that require a real tool receipt."""

    text = (user_text or "").strip()
    owner = (caller_role or "").strip().lower() == "owner"

    if _MEMORY_CLEAR_RE.search(text):
        if is_group and not owner:
            return None
        return MemoryReceiptRequirement(
            kind="memory_clear",
            successful_tools=frozenset({"clear_memory"}),
            retry_context=(
                "用户明确要求清空当前作用域记忆。必须调用 clear_memory(confirm=true)；"
                "只有工具成功后才能声称已清空。若权限不足或工具失败，如实说明。"
            ),
            failure_text="未能完成记忆清空：本轮没有成功的 clear_memory 工具事件。",
        )

    if not _MEMORY_EXPLICIT_RE.search(text):
        return None
    if re.search(
        r"(?:记住|保存).{0,20}(?:你以后就是|你以后是|你要扮演|人格|人设)",
        text,
        re.IGNORECASE,
    ):
        return None
    decision = evaluate_memory_content(text, scope="group" if is_group else "user")
    if not decision.allowed:
        return MemoryReceiptRequirement(
            kind="memory_rejected",
            successful_tools=frozenset({"append_memory"}),
            retry_context="",
            failure_text=f"未保存这条记忆：{decision.reason}",
            retry_allowed=False,
        )
    return MemoryReceiptRequirement(
        kind="memory_append",
        successful_tools=frozenset({"append_memory"}),
        retry_context=(
            "用户明确要求记住合格内容。必须调用 append_memory 写入可信运行时选择的"
            "当前作用域；只有工具成功后才能说“已记住”。不要只口头承诺。"
        ),
        failure_text="未能保存这条记忆：本轮没有成功的 append_memory 工具事件。",
    )


__all__ = ["MemoryReceiptRequirement", "classify_memory_receipt_requirement"]
