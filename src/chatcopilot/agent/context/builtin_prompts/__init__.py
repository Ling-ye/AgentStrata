"""框架内置的中性提示词默认。

这些片段描述「每个机器人天然拥有」的通用机制行为——长期记忆 / 角色权限 /
安全信息边界——**不含**任何机器人专属内容（领域范围、具体业务工具名、产品身份）。
机器人在 BotSpec 的 ``prompts`` 段提供对应文件即可覆盖；不提供则使用这里的默认。

只读纯文本，不 import 任何枚举 / ``Workspace``，保持 agent 层平台中立。具体「按
Role / AssistantMode 取哪一段」的枚举映射由平台装配器（如
``platforms/feishu/persona.py``）完成；本模块只按字符串 key 取文件。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent

#: 合法的角色 key（与 ``role_<key>.md`` 文件名对应）。
ROLE_KEYS = ("owner", "admin", "user")


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8-sig").strip()


def default_role(key: str) -> str:
    """返回某角色（owner/admin/user）的中性默认行为片段；未知 key 退化为 user（最严格）。"""
    normalized = (key or "user").strip().lower()
    if normalized not in ROLE_KEYS:
        normalized = "user"
    return _read(f"role_{normalized}.md")


def default_memory() -> str:
    """通用长期记忆 / 记事本规则。"""
    return _read("memory.md")


def default_safety() -> str:
    """通用安全与信息边界规则。"""
    return _read("safety.md")


def default_accuracy() -> str:
    """通用准确性与抗迎合规则。"""
    return _read("accuracy.md")


def default_search_first() -> str:
    """搜索优先原则——仅在 bot 配置了搜索工具时由 prompt_builder 注入。"""
    return _read("search_first.md")


__all__ = [
    "ROLE_KEYS",
    "default_role",
    "default_memory",
    "default_safety",
    "default_accuracy",
    "default_search_first",
]
