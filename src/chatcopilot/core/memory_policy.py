"""Small shared policy for conversation-memory write eligibility."""
from __future__ import annotations

import re
from dataclasses import dataclass


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|secret[_ -]?key|"
        r"password|passwd|cookie|authorization)\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:密码|口令|密钥|秘钥|访问令牌|会话\s*cookie)\s*(?:[:：=]|是|为)\s*\S{4,}",
        re.IGNORECASE,
    ),
    re.compile(r"(?:验证码|短信码|动态口令)\s*[:：]?\s*[A-Za-z0-9-]{4,}"),
)
_CONTROL_PATTERNS = (
    re.compile(
        r"(?:记住|以后).{0,12}(?:你以后就是|你以后是|你就是|你是|你要扮演|人格|人设)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:普通成员|用户|admin).{0,20}(?:owner|所有者|最高|无限).{0,8}(?:权限|角色)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:忽略|绕过|覆盖).{0,12}(?:权限|系统指令|安全规则|工具限制)",
        re.IGNORECASE,
    ),
    re.compile(r"\bremember\b.{0,24}\b(?:you are|act as|role|persona)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:grant|give).{0,16}\bowner\b.{0,12}\b(?:role|permission)",
        re.IGNORECASE,
    ),
)
_NON_DURABLE_PATTERN = re.compile(
    r"(?:临时|一次性|仅本次|只在本次|这次任务|当前任务|本轮|待会儿)",
    re.IGNORECASE,
)
_GROUP_PRIVATE_PATTERN = re.compile(
    r"(?:仅限你我|不要告诉群里|私聊秘密|个人隐私)|"
    r"(?:我的|本人).{0,8}(?:身份证|手机号|住址|家庭住址|病史|医疗记录|银行卡)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryContentDecision:
    allowed: bool
    reason: str = ""


def evaluate_memory_content(text: str, *, scope: str) -> MemoryContentDecision:
    """Apply deterministic exclusions; semantic stability remains model-guided."""

    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return MemoryContentDecision(
            False,
            "长期记忆不能保存凭据、验证码、令牌、私钥或会话秘密。",
        )
    if any(pattern.search(text) for pattern in _CONTROL_PATTERNS):
        return MemoryContentDecision(
            False,
            "人格、角色、授权或系统规则不能通过记忆修改。",
        )
    if _NON_DURABLE_PATTERN.search(text):
        return MemoryContentDecision(False, "临时或一次性任务内容不进入长期记忆。")
    if scope == "group" and _GROUP_PRIVATE_PATTERN.search(text):
        return MemoryContentDecision(
            False,
            "群记忆只能保存适合向当前群公开的内容。",
        )
    return MemoryContentDecision(True)


__all__ = ["MemoryContentDecision", "evaluate_memory_content"]
