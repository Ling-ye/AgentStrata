"""扫描 bots/*/bot.yaml，解析出 BotInstance 列表。

刻意不依赖 PyYAML：只需要 top-level 标量（id / display_name）、platform.type
与整个 deploy 段这几类扁平字段，用与 deploy/_load_env.sh 同源的极简解析即可，
保证控制台在最小依赖下也能跑。
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Dict, List, Optional

from console.control.instances import BotInstance


@functools.lru_cache(maxsize=1)
def repo_root() -> Path:
    """仓库根 = console/ 的上一级。控制台读取此处的 bots/ 与 deploy/ 作为源。"""
    return Path(__file__).resolve().parents[2]


def _parse_bot_yaml(path: Path) -> Optional[BotInstance]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    top: Dict[str, str] = {}
    platform_type = ""
    runtime_kind = "legacy"
    deploy: Dict[str, str] = {}
    section = ""

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        # top-level key（行首非空白）
        if not raw[:1].isspace() and ":" in line:
            key, value = line.split(":", 1)
            section = key.strip()
            value = value.strip().strip('"').strip("'")
            if value:
                top[section] = value
            if section == "gateway":
                runtime_kind = "gateway"
            continue
        # 缩进子项
        if raw[:1].isspace() and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if section == "platform" and key == "type":
                platform_type = value
            elif section == "channels" and key == "qq":
                platform_type = "qq"
            elif section == "deploy":
                deploy[key] = value

    instance_id = top.get("id") or path.parent.name
    return BotInstance.from_deploy(
        instance_id=deploy.get("instance_id") or instance_id,
        bot_spec=str(path),
        display_name=top.get("display_name", ""),
        platform=platform_type,
        deploy=deploy,
        home=Path.home(),
        runtime_kind=runtime_kind,
    )


def discover_instances(root: Optional[Path] = None) -> List[BotInstance]:
    base = (root or repo_root()) / "bots"
    out: List[BotInstance] = []
    if not base.is_dir():
        return out
    for bot_yaml in sorted(base.glob("*/bot.yaml")):
        inst = _parse_bot_yaml(bot_yaml)
        if inst is not None:
            out.append(inst)
    return out


def find_instance(instance_id: str, root: Optional[Path] = None) -> Optional[BotInstance]:
    for inst in discover_instances(root):
        if inst.instance_id == instance_id:
            return inst
    return None
