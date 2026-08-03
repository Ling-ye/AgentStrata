"""Agent 长期记忆 provider 接口。

agent 只依赖该 Protocol；具体存储方式（markdown / database / KV 等）由实现类
决定。AgentRuntime 在 ``new_session`` 时拿 provider.snapshot() 注入到 system prompt。
"""
from __future__ import annotations

from typing import Protocol


MEMORY_INITIAL_TEMPLATE = """# Memory

> 长期记忆。仅在用户告知**可复用**的偏好、默认参数、数据源、决策时写入。
> 临时对话内容不要写进来；体积过大请用 clear_memory 重置。

## facts
<!-- 用户告知的可复用事实，如默认阈值、习惯口径、常用数据源 URL -->

## decisions
<!-- 重要的处理决策与工作流偏好，如"先趋势再 diff" -->
"""


class MemoryProvider(Protocol):
    """长期记忆 provider 协议。"""

    def snapshot(self) -> str:
        """返回当前完整记忆内容；不存在时返回空串。"""

    def append(self, *, text: str, section: str) -> None:
        """把一条记忆追加到指定二级标题段。"""

    def clear(self) -> None:
        """重置为初始模板。"""


__all__ = ["MemoryProvider", "MEMORY_INITIAL_TEMPLATE"]
