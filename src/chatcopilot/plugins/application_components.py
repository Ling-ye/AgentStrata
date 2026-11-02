"""Prepare explicitly declared plugins inside one independent application's private data."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import secrets

from chatcopilot.core.application_installation import installation
from chatcopilot.core.install_artifacts import InstallError, write_private
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.deployment.component_repository import save_components
from chatcopilot.deployment.configuration import application_components, parse_service_definition
from chatcopilot.plugins.catalog import PLUGINS
from chatcopilot.plugins.repository import InstallRepository
from chatcopilot.plugins.runtime import create_manager


def component_manager(root: Path):
    return create_manager(root / ".agentstrata-data/plugins", application_root=root)


def prepare_configuration(root: Path) -> tuple[dict, ...]:
    installed = installation(root)
    if installed is None:
        raise ValueError("Application component setup requires an installation")
    requirements = application_components(root, installed.bot_relative)
    for row in requirements:
        if row["id"] not in PLUGINS:
            raise InstallError("Unknown application plugin: " + row["id"])
    if not requirements:
        return requirements
    path = root / Path(installed.bot_relative).parent / "local.env"
    environment = load_local_env_values(path, expand_home=True)
    before = dict(environment)
    for row in requirements:
        if row["id"] == "open-llm-vtuber":
            if not environment.get("AGENTSTRATA_AVATAR_GATEWAY_TOKEN"):
                environment["AGENTSTRATA_AVATAR_GATEWAY_TOKEN"] = secrets.token_urlsafe(48)
            if not environment.get("AGENTSTRATA_AVATAR_CONTROL_AUTH"):
                environment["AGENTSTRATA_AVATAR_CONTROL_AUTH"] = "Bearer " + secrets.token_urlsafe(48)
            if not environment.get("AGENTSTRATA_AVATAR_OBSERVER_TOKEN"):
                environment["AGENTSTRATA_AVATAR_OBSERVER_TOKEN"] = secrets.token_urlsafe(48)
            port = row["options"].get("port", 12393)
            environment["AGENTSTRATA_AVATAR_MCP_URL"] = f"http://127.0.0.1:{port}/agentstrata/mcp/"
            if not environment.get("CHATCOPILOT_SOURCE_URL"):
                environment["CHATCOPILOT_SOURCE_URL"] = f"http://127.0.0.1:{port}/agentstrata/source.zip"
    if environment != before:
        write_private(path, "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
                                     for key, value in environment.items()) + "\n")
    return requirements


def prepare_components(root: Path, requirements: tuple[dict, ...], *, artifacts: Path | None = None) -> None:
    if not requirements:
        return
    installed = installation(root)
    if installed is None:
        raise ValueError("Application component setup requires an installation")
    environment = load_local_env_values(installed.control / "runtime.env", expand_home=True)
    manager = component_manager(root)
    services, units, paths, plugins = [], {}, {}, []
    for row in requirements:
        plugin_id = row["id"]
        entry = PLUGINS[plugin_id]
        manager.install(plugin_id, artifacts_directory=artifacts, start=False, options=row["options"],
            host={"python": str(root / ".venv/bin/python"), "application": {"root": str(root)},
                  "model_config": environment.get("AGENTSTRATA_LLM_CONFIG", ""),
                  "source_url": environment.get("CHATCOPILOT_SOURCE_URL", "")})
        record = InstallRepository(manager.root, plugin_id).read()
        for item in record["services"]:
            original = parse_service_definition(item, root=manager.root / plugin_id)
            bound = replace(original, name=f"{plugin_id}-{original.name}")
            services.append(bound)
            units[bound.name] = manager.services.name(plugin_id, original)
        package = entry.module.split(".")[0]
        paths[package] = entry.source_directory + "/" + package
        plugins.append({"id": plugin_id, "installed_version": record["version"],
                        "status": record["status"], "url": record["url"]})
    save_components(root, services=tuple(services), units=units, component_paths=paths, plugins=plugins)
    # Presentation must be ready before the application discovers its avatar tools.
    for row in requirements:
        manager.start(row["id"], check_connections=False)
