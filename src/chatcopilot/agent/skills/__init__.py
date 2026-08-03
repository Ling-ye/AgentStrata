"""Agent 技能能力：运行时 skill 索引注册 + body 读取。

botspec/skills.py 负责 manifest 解析与数据模型（``SkillIndexEntry``），本包负责
运行时把当前 bot 的 skill 索引注册到模块级 registry，并提供按 id 读取 SKILL.md
正文的辅助函数。``read_bot_skill`` 工具的 handler 也调用本包。
"""
from chatcopilot.agent.skills.index import (
    current_skill_index,
    read_skill_body_by_id,
    set_skill_index,
)

__all__ = [
    "current_skill_index",
    "read_skill_body_by_id",
    "set_skill_index",
]
