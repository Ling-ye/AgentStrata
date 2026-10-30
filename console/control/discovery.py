"""扫描 bots/*/bot.yaml，解析出 BotInstance 列表。

刻意不依赖 PyYAML：只需要 top-level 标量（id / display_name）、platform.type
与整个 deploy 段这几类扁平字段，用与 deploy/_load_env.sh 同源的极简解析即可，
保证控制台在最小依赖下也能跑。
"""
from __future__ import annotations

import functools
import os
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from console.control.instances import BotInstance
from chatcopilot.plugins.service import observable_agents


@functools.lru_cache(maxsize=1)
def repo_root() -> Path:
    """Use the operator-selected application copy or this Console's source tree."""
    root = Path(os.environ.get("CHATCOPILOT_SOURCE_ROOT", Path(__file__).resolve().parents[2])).expanduser().resolve(strict=True)
    if not (root / "src/chatcopilot").is_dir() or not (root / "pyproject.toml").is_file():
        raise ValueError("CHATCOPILOT_SOURCE_ROOT must identify an AgentStrata application source tree")
    return root


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
            elif section == "channels" and key == "weixin":
                platform_type = "weixin"
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
    from chatcopilot.core.application_installation import installation
    repository = root or repo_root()
    installed = installation(repository)
    base = repository / "bots"
    out: List[BotInstance] = []
    if not base.is_dir():
        return out
    paths = (repository / installed.bot_relative,) if installed else sorted(base.glob("*/bot.yaml"))
    for bot_yaml in paths:
        inst = _parse_bot_yaml(bot_yaml)
        if inst is not None:
            if installed:
                locations = installed.deployment_overrides()
                inst = replace(inst, **{key: locations[key] for key in ("wsl_home", "workspace_root", "log_dir", "env_file")})
            out.append(inst)
    return out


def find_instance(instance_id: str, root: Optional[Path] = None) -> Optional[BotInstance]:
    for inst in discover_instances(root):
        if inst.instance_id == instance_id:
            return inst
    return None


def discover_observable_instances(root: Optional[Path] = None) -> List[BotInstance]:
    """Console's read-only view; deployment/health discovery remains repository-owned."""
    instances = discover_instances(root)
    for agent in observable_agents():
        instances.append(BotInstance(
            instance_id=agent.console_id, bot_spec=agent.bot_spec, display_name=agent.display_name,
            env_file=agent.env_file, runtime_kind="gateway",
            origin={"kind": "plugin", "plugin_id": agent.plugin_id,
                    "version": agent.plugin_version, "agent_id": agent.id},
        ))
    return instances


def find_observable_instance(instance_id: str) -> Optional[BotInstance]:
    return next((inst for inst in discover_observable_instances() if inst.instance_id == instance_id), None)
