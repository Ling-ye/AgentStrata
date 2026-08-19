"""Deterministic boundary for non-Owner assistant-persona switching."""
from __future__ import annotations

import re
from typing import Any

from chatcopilot.contracts.identity import Role, role_value


NON_OWNER_PERSONA_DENIED_REPLY = (
    "机器人自身人格仅限 Owner 管理，本次也不会切换。"
    "你仍可以要求我调整语言、篇幅、结构或代码格式，"
    "也可以让我创作一段独立的角色风格内容。"
)

_INDEPENDENT_CONTENT_RE = re.compile(
    r"(?:写|创作|生成|改写|润色|翻译|总结|制作).{0,16}"
    r"(?:文案|故事|台词|对话|段落|文章|邮件|诗|剧本|代码|表格)|"
    r"(?:文案|故事|台词|对话|段落|文章|邮件|诗|剧本|代码).{0,12}"
    r"(?:写|创作|生成|改写|润色|翻译)",
    re.IGNORECASE,
)
_INTERACTIVE_SWITCH_RE = re.compile(
    r"(?:从现在开始|以后|这次|接下来)|(?:跟|和|陪).{0,6}(?:我|我们).{0,6}"
    r"(?:聊天|说话|交流)|(?:回答我|和我说话|跟我聊天)",
    re.IGNORECASE,
)
_ASSISTANT_PERSONA_RE = re.compile(
    r"(?:你|机器人|助手).{0,10}(?:现在|这次|以后|从现在开始|接下来).{0,12}"
    r"(?:就是|是|作为|扮演|变成|换成|改成)|"
    r"(?:你现在是|你就是|请你扮演|让你扮演|冒充).{0,24}|"
    r"(?:这次|以后|从现在开始|接下来).{0,12}(?:人格|人设|身份).{0,12}"
    r"(?:换成|改成|设为|是|扮演)|"
    r"(?:这次|以后|从现在开始|接下来).{0,12}(?:换成|改成|使用|用).{0,12}"
    r"(?:人格|人设|身份)|"
    r"(?:以后|从现在开始|接下来).{0,20}(?:人格|人设).{0,16}"
    r"(?:聊天|说话|交流|回答)|"
    r"(?:设置|设定|修改|更改|保存).{0,20}(?:你|机器人|助手).{0,8}"
    r"(?:人格|人设|角色)|"
    r"(?:人格|人设|角色设定).{0,12}(?:设为|改成|换成)"
    r"|(?:模仿|扮演|冒充).{0,20}(?:跟|和).{0,4}我.{0,6}(?:说话|聊天|交流)"
    r"|用.{0,12}(?:人格|人设).{0,12}(?:和我说话|跟我聊天|回答我)",
    re.IGNORECASE,
)


def non_owner_persona_request_reply(session: Any, user_text: str) -> str | None:
    """Reject assistant-identity changes while preserving content/style requests."""

    if role_value(getattr(session, "role", Role.USER)) == Role.OWNER.value:
        return None
    text = re.sub(r"\s+", " ", user_text or "").strip()
    if not text:
        return None
    if not _ASSISTANT_PERSONA_RE.search(text):
        return None
    if _INDEPENDENT_CONTENT_RE.search(text) and not _INTERACTIVE_SWITCH_RE.search(text):
        return None
    return NON_OWNER_PERSONA_DENIED_REPLY


__all__ = ["NON_OWNER_PERSONA_DENIED_REPLY", "non_owner_persona_request_reply"]
