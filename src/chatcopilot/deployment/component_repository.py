"""Private service and source bindings for one installed application."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re

from chatcopilot.contracts.deployment import ServiceDefinition
from chatcopilot.core.application_installation import installation
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_file
from chatcopilot.deployment.configuration import parse_service_definition


def save_components(root: Path, *, services: tuple[ServiceDefinition, ...], units: dict,
                    component_paths: dict, plugins: list[dict]) -> None:
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit application installation is required")
    record = {"application_id": installed.bot_id, "services": [asdict(item) for item in services],
              "units": units, "component_paths": component_paths, "plugins": plugins}
    _validate(installed, record)
    write_private(installed.control / "components.json", json.dumps(record, ensure_ascii=False, sort_keys=True))


def _validate(installed, record):
    if not isinstance(record, dict) or record.get("application_id") != installed.bot_id:
        raise ValueError("Component services belong to another application")
    services = tuple(parse_service_definition(row, root=installed.root / ".agentstrata-data/plugins")
                     for row in record["services"])
    names = {service.name for service in services}
    if len(names) != len(services) or "gateway" in names or set(record["units"]) != names:
        raise ValueError("Application component service names conflict")
    for unit in record["units"].values():
        if not isinstance(unit, str) or not re.fullmatch(
                re.escape(f"agentstrata-app-{installed.bot_id}-") + r"[a-z0-9-]+\.service", unit):
            raise ValueError("Component unit escaped its application namespace")
    for name, relative in record["component_paths"].items():
        if (not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name == "chatcopilot"
                or not isinstance(relative, str) or Path(relative).is_absolute()
                or ".." in Path(relative).parts or not relative.startswith("integrations/")):
            raise ValueError("Invalid component source binding")
    return services


def load_components(root: Path) -> tuple[tuple[ServiceDefinition, ...], dict, dict, list]:
    installed = installation(root)
    if installed is None:
        return (), {}, {}, []
    path = installed.control / "components.json"
    if not path.exists() and not path.is_symlink():
        return (), {}, {}, []
    private_file(path)
    record = json.loads(path.read_text())
    return _validate(installed, record), record["units"], record["component_paths"], record["plugins"]

