"""Agent 长期记忆 provider 接口。

agent 只依赖该 Protocol；具体存储方式（markdown / database / KV 等）由实现类
决定。快照只能作为 PromptPlan 的 ``untrusted_context`` 输入，不能形成策略层。
"""
from __future__ import annotations

from typing import Protocol

from chatcopilot.contracts.persistent_state import MEMORY_INITIAL_TEMPLATE


class MemoryProvider(Protocol):
    """长期记忆 provider 协议。"""

    def snapshot(self) -> str:
        """返回当前完整记忆内容；不存在时返回空串。"""

    def append(self, *, text: str, section: str) -> None:
        """把一条记忆追加到指定二级标题段。"""

    def clear(self) -> None:
        """重置为初始模板。"""


__all__ = ["MemoryProvider", "MEMORY_INITIAL_TEMPLATE"]
