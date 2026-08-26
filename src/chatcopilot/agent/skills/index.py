"""Read skill bodies from caller-owned immutable indexes."""

from __future__ import annotations

from typing import Iterable

from chatcopilot.contracts.skills import SkillIndexEntry, read_skill_body


def read_skill_body_from_index(
    entries: Iterable[SkillIndexEntry],
    skill_id: str,
) -> tuple[SkillIndexEntry, str]:
    """Read one body from the caller-owned index without process-global state."""
    index = tuple(entries)
    if not index:
        raise ValueError(
            "本机器人未注册任何 skill（context.playbooks.manifest 为空或未声明 playbooks.reader）"
        )
    for entry in index:
        if entry.id == skill_id:
            return entry, read_skill_body(entry)
    available = ", ".join(entry.id for entry in index) or "(none)"
    raise ValueError(f"未注册的 skill_id={skill_id!r}；当前可用 skill: {available}")


__all__ = ["read_skill_body_from_index"]
