"""长期记忆工具：read_memory / append_memory / clear_memory。

存储后端通过 ``MarkdownMemoryProvider`` 绑定到当前会话的 ``MEMORY.md``；本工具
仅做参数校验 + 友好的 LLM 回包格式化，不持有具体路径逻辑。
"""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.agent.memory.markdown import (
    MEMORY_MAX_ITEM_CHARS,
    MEMORY_SECTIONS,
    MarkdownMemoryProvider,
)
from chatcopilot.agent.tools.workspace_context import describe_workspace, resolve_workspace
from chatcopilot.external_tools.shared.spec_helpers import require_arg
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef


def _provider() -> MarkdownMemoryProvider:
    ws = resolve_workspace(create=True)
    return MarkdownMemoryProvider(ws.memory_file)


def _handler_read_memory(args: Dict[str, Any]) -> HandlerResult:
    ws = resolve_workspace(create=True)
    target = ws.memory_file
    if not target.is_file():
        return (
            f"{describe_workspace(ws)}\nMEMORY.md 不存在（或首次初始化失败），可调用 append_memory 写入第一条。",
            [str(target)],
            None,
        )
    provider = MarkdownMemoryProvider(target)
    text = provider.snapshot()
    size = target.stat().st_size
    return (
        f"{describe_workspace(ws)}\nMEMORY.md ({size} bytes)\n----\n{text}",
        [str(target)],
        None,
    )


def _handler_append_memory(args: Dict[str, Any]) -> HandlerResult:
    text = require_arg(args, "text")
    section = (args.get("section") or "facts").strip() or "facts"
    ws = resolve_workspace(create=True)
    provider = MarkdownMemoryProvider(ws.memory_file)
    provider.append(text=text, section=section)
    return (
        f"已追加到 {ws.relpath(ws.memory_file)} 的 ## {section} 段。",
        [str(ws.memory_file)],
        None,
    )


def _handler_clear_memory(args: Dict[str, Any]) -> HandlerResult:
    confirm = bool(args.get("confirm", False))
    if not confirm:
        raise ValueError("拒绝清空：clear_memory 需要 confirm=true 才会执行。")
    ws = resolve_workspace(create=True)
    provider = MarkdownMemoryProvider(ws.memory_file)
    provider.clear()
    return (
        f"MEMORY.md 已重置为初始模板：{ws.relpath(ws.memory_file)}",
        [str(ws.memory_file)],
        None,
    )


TOOLS: List[ToolDef] = [
    ToolDef(
        name="read_memory",
        summary=(
            "读取当前用户工作目录下的 MEMORY.md 全文（长期记忆）。"
            "每个新会话开局应主动调用一次，了解用户既有的偏好 / 默认参数 / 常用数据源。"
            "记忆是 per-user 隔离的，不会泄漏给其他用户。"
        ),
        properties={},
        required=[],
        handler=_handler_read_memory,
        aliases=["mem", "查看记忆"],
        category="agent.memory",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="append_memory",
        summary=(
            "把一条可复用的事实/决策追加到 MEMORY.md。"
            "仅当用户告诉你**可复用**的信息时使用（例如：'我的默认阈值是 0.3'、'我习惯先看趋势再 diff'、'数据源是 xxx URL'）。"
            "**不要**把临时对话内容（一次性问答、闲聊）写进来。"
            "条目格式由系统自动加时间戳前缀；section 推荐 facts / decisions / sources。"
        ),
        properties={
            "text": {
                "type": "string",
                "description": f"要记住的内容（一行内 ≤ {MEMORY_MAX_ITEM_CHARS} 字符；多行会被压成单行字面 \\n）。",
            },
            "section": {
                "type": "string",
                "description": (
                    "二级标题分类："
                    "'facts'=用户偏好 / 默认阈值 / 习惯口径；"
                    "'decisions'=工作流偏好；"
                    "'sources'=常用数据源 URL。"
                    "未指定时默认 facts。"
                ),
                "enum": list(MEMORY_SECTIONS),
                "default": "facts",
            },
        },
        required=["text"],
        handler=_handler_append_memory,
        aliases=["记下", "remember"],
        category="agent.memory",
        owner="agent",
        module=__name__,
    ),
    ToolDef(
        name="clear_memory",
        summary=(
            "清空当前用户的 MEMORY.md，重置为初始模板。"
            "**破坏性操作**：仅当用户明确说'清空记忆 / 忘掉之前的'时才用，且必须把 confirm 显式设为 true。"
        ),
        properties={
            "confirm": {
                "type": "boolean",
                "description": "必须显式设为 true 才会执行；false / 缺失则拒绝。",
                "default": False,
            },
        },
        required=["confirm"],
        handler=_handler_clear_memory,
        aliases=["忘掉记忆", "重置记忆"],
        category="agent.memory",
        owner="agent",
        module=__name__,
    ),
]


__all__ = ["TOOLS"]
