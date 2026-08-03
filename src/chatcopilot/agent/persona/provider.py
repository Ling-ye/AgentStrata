"""Agent 个性（persona）provider 接口。

persona 描述机器人对当前对象「该用什么人格/语气/风格」说话。与 memory 同源：
纯 markdown 落盘、无数据库。区别是 persona 是**分层**的（全局 → 群 → 个人），
读取时合并、写入时只针对单层文件。agent 只依赖该 Protocol；middleware 负责把
分层快照注入 system prompt。
"""
from __future__ import annotations

from typing import Protocol


PERSONA_FILENAME = "PERSONA.md"

# 单层 persona 文件体积上限。persona 应当精简（人格设定而非长文），故远小于 memory。
PERSONA_MAX_BYTES = 8 * 1024
# 单次 append 内容上限。
PERSONA_MAX_ITEM_CHARS = 2000

PERSONA_INITIAL_TEMPLATE = """# Persona

> 机器人对当前对象的人格设定（语气 / 风格 / 称呼 / 立场）。
> 仅 owner 可通过对话修改；普通用户只能查看。
> 分层生效：全局 → 群 → 个人，越具体的层优先级越高。

"""


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
