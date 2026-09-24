"""Conservative classification of explicit conversation-memory requests."""
from __future__ import annotations

import re

_SAVE = re.compile(
    r"(?:请)?记住|记下来|写入记忆|保存(?:下来|为偏好|到记忆)|以后按(?:这个|此|这条)",
    re.IGNORECASE,
)
_FORGET = re.compile(r"清空|删除|重置|忘掉|忘记", re.IGNORECASE)
_NEGATED = re.compile(
    r"(?:不要|别|无需|禁止|不能|不准|无需|不要再).{0,12}(?:清空|删除|重置|忘掉|忘记)",
    re.IGNORECASE,
)
_TARGETED = re.compile(r"记忆(?:里|中|中的|里的|关于)|(?:这|那|某|一)条|其中|部分|某个", re.IGNORECASE)
_ALL = re.compile(
    r"(?:清空|重置|删除|忘掉|忘记)(?:当前|本群|我的|全部|所有|整个|整份|的)*"
    r"(?:长期)?记忆(?:全部|所有)?|"
    r"(?:清空|重置|删除|忘掉|忘记)(?:全部|所有|整个|整份)(?:长期)?记忆",
    re.IGNORECASE,
)
_PERSONA = re.compile(
    r"(?:记住|保存).{0,20}(?:你以后就是|你以后是|你要扮演|人格|人设)",
    re.IGNORECASE,
)


def classify_memory_intent(text: str) -> str:
    """Return append, clear, targeted_forget, or none; ambiguous wipes stay none."""
    value = (text or "").strip()
    if not value:
        return "none"
    if _FORGET.search(value) and not _NEGATED.search(value):
        if _TARGETED.search(value):
            return "targeted_forget"
        if _ALL.search(value):
            return "clear"
    if _SAVE.search(value) and not _PERSONA.search(value):
        return "append"
    return "none"


__all__ = ["classify_memory_intent"]
