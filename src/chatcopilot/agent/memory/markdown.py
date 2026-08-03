"""Markdown 长期记忆 provider 实现。

存储后端是 ``<workspace>/MEMORY.md``，由模块级常量 ``MEMORY_INITIAL_TEMPLATE``
作为首次创建模板。读取上限 32KB，单条追加上限 1000 字符；超限抛
``ValueError``，由工具层把异常包成 LLM 可读的 ToolResult.error。
"""
from __future__ import annotations

import time
from pathlib import Path

from chatcopilot.agent.memory.provider import MEMORY_INITIAL_TEMPLATE, MemoryProvider

MEMORY_MAX_BYTES = 32 * 1024
MEMORY_MAX_ITEM_CHARS = 1000
MEMORY_SECTIONS: tuple[str, ...] = ("facts", "decisions", "sources")


class MarkdownMemoryProvider(MemoryProvider):
    """把记忆持久化到一份 markdown 文件。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> str:
        if not self.path.is_file():
            return ""
        size = self.path.stat().st_size
        if size > MEMORY_MAX_BYTES:
            raise ValueError(
                f"MEMORY.md 体积 {size} 字节超过上限 {MEMORY_MAX_BYTES}，"
                "请用 clear_memory(confirm=true) 清理后重写。"
            )
        return self.path.read_text(encoding="utf-8", errors="replace")

    def append(self, *, text: str, section: str) -> None:
        stripped = (text or "").strip()
        if not stripped:
            raise ValueError("text 不能为空")
        if len(stripped) > MEMORY_MAX_ITEM_CHARS:
            raise ValueError(
                f"text 长度 {len(stripped)} 超过单条上限 {MEMORY_MAX_ITEM_CHARS}，请精简后再写。"
            )
        section_normalized = (section or "facts").strip() or "facts"
        if section_normalized not in MEMORY_SECTIONS:
            raise ValueError(
                f"section 只能是 {', '.join(MEMORY_SECTIONS)}；收到 {section!r}"
            )

        text_oneline = stripped.replace("\r", "").replace("\n", " \\n ")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(MEMORY_INITIAL_TEMPLATE, encoding="utf-8")

        body = self.path.read_text(encoding="utf-8", errors="replace")
        header = f"## {section_normalized}"
        ts = time.strftime("%Y-%m-%d %H:%M")
        new_line = f"- {ts} {text_oneline}"
        if header in body:
            body = _insert_line_under_section(body, header, new_line)
        else:
            if not body.endswith("\n"):
                body += "\n"
            body += f"\n{header}\n{new_line}\n"

        new_size = len(body.encode("utf-8"))
        if new_size > MEMORY_MAX_BYTES:
            raise ValueError(
                f"写入后 MEMORY.md 体积 {new_size} 字节会超过上限 {MEMORY_MAX_BYTES}，"
                "请先用 clear_memory(confirm=true) 清理。"
            )
        self.path.write_text(body, encoding="utf-8")

    def clear(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(MEMORY_INITIAL_TEMPLATE, encoding="utf-8")


def _insert_line_under_section(body: str, header: str, new_line: str) -> str:
    """把 ``new_line`` 插入到 markdown 二级标题 ``header`` 段落末尾。"""
    lines = body.splitlines()
    insert_at = len(lines)
    in_section = False
    for idx, line in enumerate(lines):
        if line.strip() == header:
            in_section = True
            insert_at = idx + 1
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("## "):
                insert_at = idx
                while insert_at > 0 and not lines[insert_at - 1].strip():
                    insert_at -= 1
                break
            insert_at = idx + 1
    lines.insert(insert_at, new_line)
    result = "\n".join(lines)
    if not result.endswith("\n"):
        result += "\n"
    return result


__all__ = ["MarkdownMemoryProvider", "MEMORY_INITIAL_TEMPLATE", "MEMORY_SECTIONS"]
