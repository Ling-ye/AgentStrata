"""Resolve one configured bot and call its public schedule service."""
from pathlib import Path

from chatcopilot.botspec.provisioning import read_private_env_file
from chatcopilot.schedules.models import ScheduleError
from chatcopilot.schedules.service import ScheduleService
from console.control.instances import BotInstance
from console.control.yaml_io import load_yaml_mapping_or_empty


def service_for(inst: BotInstance) -> ScheduleService:
    if inst.runtime_kind != "gateway" or inst.platform != "qq":
        raise ScheduleError("unavailable", "定时推送需要使用原生 QQ Gateway 的机器人")
    spec = Path(inst.bot_spec)
    config = load_yaml_mapping_or_empty(spec)
    env = Path(inst.env_file) if inst.env_file else spec.parent / "local.env"
    values = read_private_env_file(env, allowed_parent=env.parent)
    key = (config.get("gateway") or {}).get("state_root_env") or "CHATCOPILOT_GATEWAY_STATE_ROOT"
    value = values.get(key, "")
    root = Path(value)
    if not value or not root.is_absolute() or not root.is_dir():
        raise ScheduleError("unavailable", "机器人尚未初始化 Gateway 状态目录，请先完成部署并启动")
    return ScheduleService(root)
