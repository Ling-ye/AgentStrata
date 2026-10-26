"""Write assembled test declarations using the current two-document format."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from chatcopilot.core.application_configuration import AGENT_CONFIGURATION_FIELDS, split_configuration


def write_application(path: Path, declaration: str | dict[str, Any], *, encoding: str = "utf-8") -> int:
    """Keep domain fixtures readable while emitting explicit application/profile files."""
    data = yaml.safe_load(declaration) if isinstance(declaration, str) else declaration
    if not isinstance(data, dict):
        return path.write_text(declaration if isinstance(declaration, str) else yaml.safe_dump(data), encoding=encoding)
    if "agent_profile" in data and not AGENT_CONFIGURATION_FIELDS.intersection(data):
        return path.write_text(declaration if isinstance(declaration, str) else yaml.safe_dump(data), encoding=encoding)
    filename = "agent.yaml" if path.name == "bot.yaml" else path.stem + ".agent.yaml"
    filename = data.get("agent_profile", filename)
    application, profile = split_configuration({key: value for key, value in data.items() if key != "agent_profile"}, profile=filename)
    (path.parent / filename).write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding=encoding)
    return path.write_text(yaml.safe_dump(application, allow_unicode=True, sort_keys=False), encoding=encoding)
