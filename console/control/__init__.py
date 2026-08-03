"""控制契约层：把 deploy/wsl 脚本与 systemd --user 封装成稳定的 JSON 契约。

后端（console.backend）与 CLI（python -m console.control）都只依赖这里暴露的
函数与 dataclass，永远不直接解析脚本的人类可读输出，从而做到三层解耦。
"""
from __future__ import annotations

from console.control.discovery import discover_instances, find_instance, repo_root
from console.control.instances import BotInstance
from console.control import operations

__all__ = [
    "BotInstance",
    "discover_instances",
    "find_instance",
    "repo_root",
    "operations",
]
