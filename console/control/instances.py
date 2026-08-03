"""BotInstance 数据模型 + 路径解析。

字段与 bots/<id>/bot.yaml 的 deploy 段一一对应，路径在此统一展开 ~ 并派生
cc_home / 日志文件等运行时位置，供上层（status/logs/jobs）复用。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional


def _expand(value: str, home: Path) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value == "~":
        return str(home)
    if value.startswith("~/"):
        return str(home / value[2:])
    return value


@dataclass
class BotInstance:
    """单个机器人实例的静态配置 + 派生路径。"""

    instance_id: str
    bot_spec: str
    display_name: str = ""
    platform: str = ""
    wsl_home: str = ""
    workspace_root: str = ""
    log_dir: str = ""
    env_file: str = ""
    cc_connect_config_dir: str = ""
    cc_home: str = ""
    project_name: str = ""

    # systemd 模板服务名
    @property
    def unit(self) -> str:
        return f"chatcopilot@{self.instance_id}.service"

    @property
    def unit_short(self) -> str:
        return f"chatcopilot@{self.instance_id}"

    def deploy_script(self, name: str) -> str:
        """实例自身副本里的 deploy/wsl/<name>（按所在目录推导 instance）。"""
        return str(Path(self.wsl_home) / "deploy" / "wsl" / name)

    @property
    def is_deployed(self) -> bool:
        return bool(self.wsl_home) and Path(self.wsl_home, "deploy", "wsl").is_dir()

    def cc_log_file(self) -> Optional[str]:
        """cc-connect 当日主日志：<log_dir>/cc-connect/<YYYY-MM-DD>.log，回退 current.log。"""
        if not self.log_dir:
            return None
        cc_dir = Path(self.log_dir) / "cc-connect"
        today = cc_dir / f"{date.today().isoformat()}.log"
        if today.exists():
            return str(today)
        current = cc_dir / "current.log"
        if current.exists():
            return str(current)
        if today.parent.is_dir():
            return str(today)
        return "/tmp/cc-connect.log"

    def questions_log_file(self) -> Optional[str]:
        if not self.log_dir:
            return None
        return str(Path(self.log_dir) / f"{date.today().isoformat()}.log")

    def runtime_log_file(self) -> Optional[str]:
        if not self.log_dir:
            return None
        return str(Path(self.log_dir) / "runtime" / f"{date.today().isoformat()}.log")

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["unit"] = self.unit_short
        data["is_deployed"] = self.is_deployed
        return data

    @classmethod
    def from_deploy(
        cls,
        *,
        instance_id: str,
        bot_spec: str,
        display_name: str,
        platform: str,
        deploy: Dict[str, str],
        home: Path,
    ) -> "BotInstance":
        wsl_home = _expand(deploy.get("wsl_home", ""), home) or str(home / f"ChatCopilot-{instance_id}")
        cc_cfg = _expand(deploy.get("cc_connect_config_dir", ""), home)
        cc_home = ""
        if cc_cfg.endswith("/.cc-connect"):
            cc_home = cc_cfg[: -len("/.cc-connect")] or str(home)
        elif cc_cfg:
            cc_home = str(Path(cc_cfg).parent)
        return cls(
            instance_id=instance_id,
            bot_spec=bot_spec,
            display_name=display_name or instance_id,
            platform=platform,
            wsl_home=wsl_home,
            workspace_root=_expand(deploy.get("workspace_root", ""), home),
            log_dir=_expand(deploy.get("log_dir", ""), home),
            env_file=_expand(deploy.get("env_file", ""), home),
            cc_connect_config_dir=cc_cfg,
            cc_home=cc_home,
            project_name=deploy.get("project_name", ""),
        )
