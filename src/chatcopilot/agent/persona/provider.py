"""Agent 个性（persona）provider 接口。

persona 描述机器人对当前对象「该用什么人格/语气/风格」说话。与 memory 同源：
纯 markdown 落盘、无数据库。区别是 persona 是**分层**的（全局 → 群 → 个人），
读取时合并、写入时只针对单层文件。agent 只依赖该 Protocol；middleware 负责把
分层快照注入 system prompt。
"""
from __future__ import annotations

from typing import Protocol

from chatcopilot.contracts.persistent_state import (
    PERSONA_INITIAL_TEMPLATE,
    PERSONA_MAX_BYTES,
    PERSONA_MAX_ITEM_CHARS,
)


PERSONA_FILENAME = "PERSONA.md"


class PersonaProvider(Protocol):
    """个性 provider 协议（单层文件视角）。"""

    def snapshot(self) -> str:
        """返回该层 persona 文件全文；不存在时返回空串。"""

    def set(self, text: str) -> None:
        """用 ``text`` 覆盖该层 persona 文件。"""

    def append(self, text: str) -> None:
        """把 ``text`` 追加到该层 persona 文件末尾。"""

    def clear(self) -> None:
        """删除该层 persona 设定（重置为初始模板）。"""


__all__ = [
    "PersonaProvider",
    "PERSONA_FILENAME",
    "PERSONA_INITIAL_TEMPLATE",
    "PERSONA_MAX_BYTES",
    "PERSONA_MAX_ITEM_CHARS",
]
