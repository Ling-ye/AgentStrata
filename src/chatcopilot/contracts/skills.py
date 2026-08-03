"""Skill index contracts and pure rendering helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class SkillIndexEntry:
    """Skill index entry: id, display metadata, and SKILL.md path."""

    id: str
    name: str
    description: str
    body_path: Path


def read_skill_body(entry: SkillIndexEntry) -> str:
    """Read SKILL.md body without YAML frontmatter."""
    raw = entry.body_path.read_text(encoding="utf-8")
    return _strip_frontmatter(raw).strip() + "\n"


def render_skill_index_section(entries: Sequence[SkillIndexEntry]) -> str:
    """Render skill index entries for prompt injection."""
    if not entries:
        return ""
    lines = [
        "## 可用 Skills（按需读取详细流程）",
        "",
        "下列 skill 对应的详细执行规则不在 system prompt 中，需要在判定触发条件后调用 `read_bot_skill(skill_id=...)` 读取：",
        "",
    ]
    for entry in entries:
        description = entry.description.strip()
        lines.append(f"- `{entry.id}` **{entry.name}** —— {description}")
    lines.append("")
    return "\n".join(lines)


def _strip_frontmatter(raw: str) -> str:
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return raw
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return raw
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1 :])
    return raw


__all__ = [
    "SkillIndexEntry",
    "read_skill_body",
    "render_skill_index_section",
]
