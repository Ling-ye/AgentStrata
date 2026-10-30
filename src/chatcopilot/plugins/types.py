"""Plugin installation and service descriptions; no executable plugin imports."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from chatcopilot.core.install_artifacts import InstallError


@dataclass(frozen=True)
class ObservableAgentDefinition:
    id: str
    display_name: str
    bot_spec: str
    env_file: str

    @classmethod
    def parse(cls, value: dict) -> ObservableAgentDefinition:
        if not isinstance(value, dict) or set(value) != {"id", "display_name", "bot_spec", "env_file"}:
            raise InstallError("Invalid observable Agent description")
        if any(not isinstance(item, str) or not item or any(c in item for c in "\r\n\0")
               for item in value.values()):
            raise InstallError("Observable Agent fields must be nonempty strings")
        agent = cls(**value)
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", agent.id):
            raise InstallError("Invalid observable Agent id")
        for path in (agent.bot_spec, agent.env_file):
            if Path(path).is_absolute() or ".." in Path(path).parts or "\\" in path or path == ".":
                raise InstallError("Observable Agent paths must be installation-relative")
        return agent


@dataclass(frozen=True)
class InstalledAgent:
    plugin_id: str
    plugin_version: str
    id: str
    display_name: str
    bot_spec: str
    env_file: str

    @property
    def console_id(self) -> str:
        return f"plugin:{self.plugin_id}:{self.id}"


@dataclass(frozen=True)
class PluginDefinition:
    id: str
    package: str
    version: str
    module: str
    python: str
    description: str
    requires_agent: bool = False
    source_directory: str = ""

    @property
    def wheel(self) -> str:
        return f"{self.package.replace('-', '_')}-{self.version}-py3-none-any.whl"
