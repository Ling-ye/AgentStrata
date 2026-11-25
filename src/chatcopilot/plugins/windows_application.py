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


def _invoke(root: Path, python: Path, source: str, plugin: Path, args: list[str]) -> str:
    if not python.is_file():
        raise ValueError("Native plugin interpreter is not prepared")
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / source)))}
    result = subprocess.run([str(python), *args], env=environment, check=False, capture_output=True,
                            text=True, encoding="utf-8")
    if result.returncode:
        write_private(plugin / "logs/setup.log", result.stderr)
        raise RuntimeError("Native component operation failed; inspect the application's private setup.log")
    return result.stdout


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
    services, units, paths, plugins = [], {}, {}, []
    for requirement in requirements:
        plugin_id = requirement["id"]
        entry = PLUGINS[plugin_id]
        plugin = root / ".agentstrata-data/plugins" / plugin_id
        python = plugin / "runtime/venv/Scripts/python.exe"
        options = requirement["options"]
        port = options["port"]
        url = f"http://127.0.0.1:{port}"
        if plugin_id == "live2d-generator":
            from chatcopilot.core.install_artifacts import private_directory
            config = plugin / "config/generator.json"
            private_directory(plugin / "config")
            request = plugin / "config/native-request.json"
            write_private(request, json.dumps({"application": str(root), "plugin": str(plugin),
                                              "options": options}, ensure_ascii=False))
            _invoke(root, python, entry.source_directory, plugin,
                    ["-m", entry.module, "prepare-windows", "--request", str(request)])
            command = (str(python), "-m", "agentstrata_live2d.cli", "serve", "--root",
                       str(plugin / "data/jobs"), "--config", str(config))
            service = ServiceDefinition("live2d-generator-studio", command, str(plugin),
                str(plugin / "config/runtime.env"), port, url + "/health", True)
        elif plugin_id == "open-llm-vtuber":
            request = {"root": str(plugin), "plugin": {"id": plugin_id, "version": entry.version},
                "options": options, "host": {"python": str(root / ".venv/Scripts/python.exe"),
                    "application": {"root": str(root)}, "source_url": environment["CHATCOPILOT_SOURCE_URL"]},
                "artifacts": str(artifacts) if artifacts else ""}
            request_file = plugin / "config/prepare.json"
            write_private(request_file, json.dumps(request, ensure_ascii=False))
            _invoke(root, python, entry.source_directory, plugin,
                    ["-m", entry.module, "prepare", "--request", str(request_file)])
            env_file = plugin / "config/runtime.env"
            original = load_local_env_values(env_file, expand_home=False)
            original.update({name: value for name, value in environment.items()
                             if name in {"AGENTSTRATA_GPT_SOVITS_URL", "AGENTSTRATA_GPT_SOVITS_AUTH",
                                         "AGENTSTRATA_INDEX_TTS_URL", "AGENTSTRATA_INDEX_TTS_AUTH"}})
            write_private(env_file, "\n".join(name + "=" + json.dumps(value, ensure_ascii=False)
                                               for name, value in original.items()) + "\n")
            service = ServiceDefinition("open-llm-vtuber-client",
                (str(python), "-m", entry.module, "run", "--request", str(request_file)), str(plugin),
                str(plugin / "config/runtime.env"), port, url + "/", True)
        else:
            raise ValueError("Unsupported native component: " + plugin_id)
        services.append(service)
        units[service.name] = f"agentstrata-app-{installed.bot_id}-{service.name}.service"
        package = entry.module.split(".")[0]
        paths[package] = entry.source_directory + "/" + package
        plugins.append({"id": plugin_id, "installed_version": entry.version, "status": "ready", "url": url})
        write_private(plugin / "installation.json", json.dumps({"plugin_id": plugin_id, "root": str(plugin),
            "version": entry.version, "status": "ready", "url": url, "services": [asdict(service)]}))
    save_components(root, services=tuple(services), units=units, component_paths=paths, plugins=plugins)
    return {"ok": True, "state": "needs_model_configuration", "root": str(root)}


def change_character(root: Path, command: str, values: dict) -> dict:
    if os.name != "nt" or installation(root) is None:
        raise ValueError("Character actions require an installed Windows companion")
    if not isinstance(values, dict) or set(values) != ({"id", "version"} if command == "character-apply" else {"id"}):
        raise ValueError("Invalid character action")
    from chatcopilot.deployment.component_repository import load_components
    _, _, _, plugins = load_components(root)
    if not any(item["id"] == "live2d-generator" and item["status"] == "ready" for item in plugins):
        raise ValueError("Prepare the character studio for this application first")
    studio = root / ".agentstrata-data/plugins/live2d-generator"
    target = root / ".agentstrata-data/plugins/open-llm-vtuber"
    python = studio / "runtime/venv/Scripts/python.exe"
    args = ["-m", "agentstrata_live2d.cli"]
    if command == "character-apply":
        args += ["characters", "apply", "--root", str(studio / "data/jobs"), "--id", str(values["id"]),
                 "--version", str(values["version"]), "--vtuber-root", str(target)]
    else:
        args += ["restore", "--backup", str(values["id"]), "--vtuber-root", str(target)]
    return json.loads(_invoke(root, python, PLUGINS["live2d-generator"].source_directory, studio, args))
