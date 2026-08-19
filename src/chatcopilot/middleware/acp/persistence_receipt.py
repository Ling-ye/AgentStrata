"""Bounded receipt requirements for explicit persona and memory mutations."""
from __future__ import annotations

import re
from dataclasses import dataclass

from chatcopilot.core.memory_policy import evaluate_memory_content


_PERSONA_NOUN = r"(?:人格|人设|persona|角色设定|说话人设)"
_PERSONA_PERSIST_RE = re.compile(
    rf"(?:设置|设定|修改|更改|换成|切换|保存|固定|以后).{{0,24}}{_PERSONA_NOUN}|"
    rf"{_PERSONA_NOUN}.{{0,16}}(?:设为|设置|改成|换成)|"
    r"(?:记住|保存).{0,16}(?:你以后就是|你以后是|你要扮演|你就是)",
    re.IGNORECASE,
)
_PERSONA_CLEAR_RE = re.compile(rf"(?:清空|删除|重置|恢复默认).{{0,16}}{_PERSONA_NOUN}", re.IGNORECASE)
_MEMORY_EXPLICIT_RE = re.compile(
    r"(?:请)?记住|记下来|写入记忆|保存(?:下来|为偏好|到记忆)|以后按(?:这个|此|这条)",
    re.IGNORECASE,
)
_MEMORY_CLEAR_RE = re.compile(r"(?:清空|删除|重置|忘掉).{0,12}(?:记忆|长期记忆)", re.IGNORECASE)


@dataclass(frozen=True)
class PersistenceReceiptRequirement:
    kind: str
    successful_tools: frozenset[str]
    retry_appendix: str
    failure_text: str
    retry_allowed: bool = True


def classify_persistence_requirement(
    user_text: str,
    *,
    caller_role: str,
    is_group: bool = False,
) -> PersistenceReceiptRequirement | None:
    """Classify explicit persistence requests and deterministic rejections.

    This is intentionally narrow. Implicit reusable information still belongs to
    the model's one-time confirmation flow and does not trigger an automatic retry.
    """

    text = (user_text or "").strip()
    owner = (caller_role or "").strip().lower() == "owner"

    if owner and _PERSONA_CLEAR_RE.search(text):
        return _persona_requirement(frozenset({"persona_clear"}))
    if owner and _PERSONA_PERSIST_RE.search(text):
        return _persona_requirement(frozenset({"persona_set", "persona_append"}))

    if _MEMORY_CLEAR_RE.search(text):
        if is_group and not owner:
            return None
        return PersistenceReceiptRequirement(
            kind="memory_clear",
            successful_tools=frozenset({"clear_memory"}),
            retry_appendix=(
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
        # Non-Owner persona requests are rejected by the deterministic persona
        # boundary before the model turn; they must not be misrouted to memory.
        return None
    decision = evaluate_memory_content(text, scope="group" if is_group else "user")
    if not decision.allowed:
        return PersistenceReceiptRequirement(
            kind="memory_rejected",
            successful_tools=frozenset({"append_memory"}),
            retry_appendix="",
            failure_text=f"未保存这条记忆：{decision.reason}",
            retry_allowed=False,
        )
    return PersistenceReceiptRequirement(
        kind="memory_append",
        successful_tools=frozenset({"append_memory"}),
        retry_appendix=(
            "用户明确要求记住合格内容。必须调用 append_memory 写入可信运行时选择的"
            "当前作用域；只有工具成功后才能说“已记住”。不要只口头承诺。"
        ),
        failure_text="未能保存这条记忆：本轮没有成功的 append_memory 工具事件。",
    )


def _persona_requirement(
    tools: frozenset[str],
) -> PersistenceReceiptRequirement:
    return PersistenceReceiptRequirement(
        kind="persona",
        successful_tools=tools,
        retry_appendix=(
            "这是 Owner 的明确持久化人格指令。必须调用匹配的 persona 工具，群聊默认"
            " scope=group、私聊默认 scope=user；只有工具成功后才能声称人格已修改。"
            "保留 Owner 指定的模仿或直接角色扮演强度，不要改写为相近原创风格。"
        ),
        failure_text="未能保存人格修改：本轮没有成功的 persona 持久化工具事件。",
    )


__all__ = [
    "PersistenceReceiptRequirement",
    "classify_persistence_requirement",
]
