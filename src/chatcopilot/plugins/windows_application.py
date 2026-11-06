"""Windows software composition called by the public application entrypoint."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess

from chatcopilot.contracts.deployment import ServiceDefinition
from chatcopilot.core.application_installation import installation
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.deployment.component_repository import save_components
from chatcopilot.plugins.catalog import PLUGINS


def setup(root: Path, *, artifacts: Path | None, requirements=(), dry_run=False) -> dict:
    installed = installation(root)
    if installed is None or os.name != "nt":
        raise ValueError("Native setup requires an installed Windows application")
    if dry_run:
        return {"state": "planned", "root": str(root), "platform": "windows-x86_64"}
    native_python = root / "windows-mcp/venv/Scripts/python.exe"
    if requirements and not native_python.is_file():
        raise ValueError("Prepare the pinned Windows-MCP environment through the native bootstrap first")
    environment = load_local_env_values(installed.control / "runtime.env", expand_home=True)
    services, units, plugins = [], {}, []
    for requirement in requirements:
        plugin_id = requirement["id"]
        entry = PLUGINS[plugin_id]
        plugin = root / ".agentstrata-data/plugins" / plugin_id
        python = plugin / "runtime/venv/Scripts/python.exe"
        if not python.is_file():
            raise ValueError("Native plugin interpreter is not prepared")
        request = {"root": str(plugin), "plugin": {"id": plugin_id, "version": entry.version},
            "options": requirement["options"], "host": {"python": str(root / ".venv/Scripts/python.exe"),
                "application": {"root": str(root)}, "source_url": environment["CHATCOPILOT_SOURCE_URL"]},
            "artifacts": str(artifacts) if artifacts else ""}
        request_file = plugin / "config/prepare.json"
        write_private(request_file, json.dumps(request, ensure_ascii=False))
        child_environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": os.pathsep.join(
            (str(root / "src"), str(root / entry.source_directory)))}
        result = subprocess.run([str(python), "-m", entry.module, "prepare", "--request", str(request_file)],
                                env=child_environment, check=False, capture_output=True, text=True)
        if result.returncode:
            write_private(plugin / "logs/setup.log", result.stderr)
            raise RuntimeError("Native Live2D preparation failed; inspect the application's private setup log")
        port = requirement["options"]["port"]
        service = ServiceDefinition("open-llm-vtuber-client",
            (str(python), "-m", entry.module, "run", "--request", str(request_file)), str(plugin),
            str(plugin / "config/runtime.env"), port, f"http://127.0.0.1:{port}/", True)
        services.append(service)
        units[service.name] = f"agentstrata-app-{installed.bot_id}-open-llm-vtuber-client.service"
        url = f"http://127.0.0.1:{port}"
        plugins.append({"id": plugin_id, "installed_version": entry.version, "status": "ready", "url": url})
        write_private(plugin / "installation.json", json.dumps({"plugin_id": plugin_id, "root": str(plugin),
            "version": entry.version, "status": "ready", "url": url, "services": [asdict(service)]}))
    save_components(root, services=tuple(services), units=units,
                    component_paths={"agentstrata_vtuber": "integrations/open_llm_vtuber/agentstrata_vtuber"} if requirements else {}, plugins=plugins)
    return {"ok": True, "state": "needs_model_configuration", "root": str(root)}
