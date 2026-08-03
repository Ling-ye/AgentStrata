"""Agent 进程内的 skill 索引注册表。

单进程只服务一个 bot，所以注册表是模块级的。AgentRuntime 启动期一次性写入；
``read_bot_skill`` 工具与 prompt builder 都从这里读。
"""
from __future__ import annotations

from typing import Iterable

from chatcopilot.contracts.skills import SkillIndexEntry, read_skill_body

_REGISTRY: tuple[SkillIndexEntry, ...] = ()


def set_skill_index(entries: Iterable[SkillIndexEntry]) -> None:
    """把当前 bot 的 skill 索引写入注册表。"""
    global _REGISTRY
    _REGISTRY = tuple(entries)


def current_skill_index() -> tuple[SkillIndexEntry, ...]:
    return _REGISTRY


def read_skill_body_by_id(skill_id: str) -> tuple[SkillIndexEntry, str]:
    """按 id 读取 skill 正文；找不到时抛 ValueError 并附可用 id 列表。"""
    if not _REGISTRY:
        raise ValueError(
            "本机器人未注册任何 skill（context.playbooks.manifest 为空或未声明 playbooks.reader）"
        )
    for entry in _REGISTRY:
        if entry.id == skill_id:
            return entry, read_skill_body(entry)
    available = ", ".join(entry.id for entry in _REGISTRY) or "(none)"
    raise ValueError(
        f"未注册的 skill_id={skill_id!r}；当前可用 skill: {available}"
    )


__all__ = [
    "current_skill_index",
    "read_skill_body_by_id",
    "set_skill_index",
]
