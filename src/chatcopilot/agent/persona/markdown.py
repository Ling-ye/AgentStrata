"""Markdown 个性 provider 实现（单层文件）。

每个 persona 层是一份独立的 ``PERSONA.md``。``MarkdownPersonaProvider`` 只负责
单层文件的读写与体积约束；分层合并由 :mod:`chatcopilot.agent.persona.layers`
负责，避免读写两种语义混在一个类里。
"""
from __future__ import annotations

from pathlib import Path

from chatcopilot.agent.persona.provider import (
    PERSONA_INITIAL_TEMPLATE,
    PERSONA_MAX_BYTES,
    PERSONA_MAX_ITEM_CHARS,
    PersonaProvider,
)


class MarkdownPersonaProvider(PersonaProvider):
    """把单层 persona 持久化到一份 markdown 文件。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> str:
        if not self.path.is_file():
            return ""
        size = self.path.stat().st_size
        if size > PERSONA_MAX_BYTES:
            raise ValueError(
                f"{self.path.name} 体积 {size} 字节超过单层上限 {PERSONA_MAX_BYTES}，"
                "请用 persona_set 精简后重写。"
            )
        return self.path.read_text(encoding="utf-8", errors="replace")

    def set(self, text: str) -> None:
        body = self._normalize(text)
        if not body.strip():
            raise ValueError("text 不能为空")
        self._guard_size(body)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")

    def append(self, text: str) -> None:
        addition = self._normalize(text)
        if not addition.strip():
            raise ValueError("text 不能为空")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(PERSONA_INITIAL_TEMPLATE, encoding="utf-8")
        body = self.path.read_text(encoding="utf-8", errors="replace")
        if body and not body.endswith("\n"):
            body += "\n"
        body += addition if addition.endswith("\n") else addition + "\n"
        self._guard_size(body)
        self.path.write_text(body, encoding="utf-8")

    def clear(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(PERSONA_INITIAL_TEMPLATE, encoding="utf-8")

    @staticmethod
    def _normalize(text: str) -> str:
        stripped = (text or "").strip()
        if len(stripped) > PERSONA_MAX_ITEM_CHARS:
            raise ValueError(
                f"text 长度 {len(stripped)} 超过上限 {PERSONA_MAX_ITEM_CHARS}，请精简后再写。"
            )
        return stripped.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _guard_size(body: str) -> None:
        size = len(body.encode("utf-8"))
        if size > PERSONA_MAX_BYTES:
            raise ValueError(
                f"写入后 persona 体积 {size} 字节会超过单层上限 {PERSONA_MAX_BYTES}，请精简。"
            )


__all__ = ["MarkdownPersonaProvider"]
