"""Bind presentation to an installed application; the application owns its Agent."""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from chatcopilot.core.application_installation import installation
from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.install_artifacts import InstallError, private_directory, write_private
from chatcopilot.core.settings import load_local_env_values
from .resources import resource
from .configuration_files import environment_file, persistent_link


def application_binding(request: dict):
    value = request["host"]["application"]
    if not isinstance(value, dict) or set(value) != {"root"}:
        raise InstallError("Invalid application binding")
    installed = installation(Path(value["root"]))
    if installed is None:
        raise InstallError("VTuber requires an explicit independent application")
    plugin = Path(request["root"]).absolute()
    if plugin != installed.root / ".agentstrata-data/plugins" / request["plugin"]["id"]:
        raise InstallError("VTuber directory belongs to another application")
    app = load_application_spec(installed.root / installed.bot_relative)
    env = load_local_env_values(installed.control / "runtime.env", expand_home=True)
    if int(env[app.raw["gateway"].get("port_env", "CHATCOPILOT_GATEWAY_PORT")]) != request["options"]["gateway_port"]:
        raise InstallError("VTuber Gateway port differs from its application")
    return installed, app, env


def configure_application_client(request: dict, sources: dict[str, Path]) -> None:
    installed, app, environment = application_binding(request)
    root = Path(request["root"])
    config = private_directory(root / "config")
    data = private_directory(root / "data")
    port, gateway_port = request["options"]["port"], request["options"]["gateway_port"]
    configured = config / "configured.json"
    if not configured.exists():
        values = {key: environment[key] for key in (
            "AGENTSTRATA_AVATAR_GATEWAY_TOKEN", "AGENTSTRATA_AVATAR_CONTROL_AUTH",
            "AGENTSTRATA_AVATAR_OBSERVER_TOKEN",
            "CHATCOPILOT_GATEWAY_STATE_ROOT",
        )}
        auth = values["AGENTSTRATA_AVATAR_CONTROL_AUTH"]
        if not auth.startswith("Bearer "):
            raise InstallError("Application avatar control credential is missing")
        values["AGENTSTRATA_AVATAR_CONTROL_TOKEN"] = auth.removeprefix("Bearer ")
        values.update(AGENTSTRATA_VTUBER_CONFIG=str(config / "adapter.json"),
                      AGENTSTRATA_APPLICATION_ROOT=str(installed.root),
                      CHATCOPILOT_SOURCE_URL=request["host"]["source_url"],
                      PATH=str(root / "runtime/tools/ffmpeg/bin") + ":/usr/local/bin:/usr/bin:/bin",
                      PYTHONUNBUFFERED="1")
        values["PYTHONPATH"] = os.pathsep.join((str(installed.root / "src"),
                                               str(installed.root / "integrations/open_llm_vtuber")))
        adapter = json.loads(resource("config.example.json").read_text())
        adapter["gateway_url"] = f"ws://127.0.0.1:{gateway_port}"
        adapter["allowed_origins"] = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
        write_private(config / "adapter.json", json.dumps(adapter, indent=2))
        environment_file(config / "runtime.env", values)
        upstream = yaml.safe_load((sources["backend"] / "config_templates/conf.ZH.default.yaml").read_text())
        upstream["system_config"].update(host="127.0.0.1", port=port)
        upstream["character_config"].update(conf_uid=installed.bot_id, conf_name=app.raw.get("display_name", installed.bot_id),
                                               character_name="Mao")
        upstream["character_config"]["agent_config"]["conversation_agent_choice"] = "agentstrata"
        write_private(config / "upstream.yaml", yaml.safe_dump(upstream, allow_unicode=True, sort_keys=False))
        write_private(configured, json.dumps({"port": port, "gateway_port": gateway_port,
                                             "application_root": str(installed.root)}))
    elif json.loads(configured.read_text()).get("application_root") != str(installed.root):
        raise InstallError("VTuber was configured for another application")
    for name, target in {"models": data / "models", "chat_history": data / "chat_history",
                         "logs": root / "logs/client", "cache": data / "audio-cache"}.items():
        persistent_link(sources["backend"] / name, target)
    upstream_config = sources["backend"] / "conf.yaml"
    if not upstream_config.exists():
        upstream_config.symlink_to(config / "upstream.yaml")
