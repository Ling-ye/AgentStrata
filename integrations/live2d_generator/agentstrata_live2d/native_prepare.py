"""Windows application-owned generator and voice preparation."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from chatcopilot.core.settings import load_local_env_values

from .config import Config
from .repository import private_directory, write_json, write_bytes
from .voice_install import configure_voice


def prepare(plugin: Path, application: Path, options: dict, environment: dict) -> None:
    for name in ("config", "data/jobs", "data/characters", "logs", "runtime"):
        private_directory(plugin / name)
    path = plugin / "config/generator.json"
    options_config = {"port": options["port"]}
    current = Config.load(path) if path.is_file() else Config()
    config = Config.parse(asdict(current) | options_config)
    write_json(path, asdict(config))
    path = plugin / "config/runtime.env"
    before = load_local_env_values(path, expand_home=True) if path.is_file() else {}
    names = ("CHATCOPILOT_SOURCE_URL", "AGENTSTRATA_CHARACTER_MCP_AUTH", "AGENTSTRATA_GPT_SOVITS_AUTH")
    before.update({name: environment[name] for name in names})
    before["AGENTSTRATA_LIVE2D_VTUBER_ROOT"] = str(application / ".agentstrata-data/plugins/open-llm-vtuber")
    if options.get("voice"):
        before["AGENTSTRATA_GPT_SOVITS_CONFIG"] = str(configure_voice(plugin, application, options["voice_port"]))
    write_bytes(path, ("\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
                                 for key, value in before.items()) + "\n").encode())
