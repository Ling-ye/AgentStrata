"""Plugin installation and service descriptions; no executable plugin imports."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlsplit

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

    @property
    def wheel(self) -> str:
        return f"{self.package.replace('-', '_')}-{self.version}-py3-none-any.whl"


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    command: tuple[str, ...]
    cwd: str
    environment_file: str
    port: int
    ready_url: str = ""

    @classmethod
    def parse(cls, value: dict, *, root: Path) -> ServiceDefinition:
        service = cls(name=value["name"], command=tuple(value["command"]), cwd=value["cwd"],
                      environment_file=value["environment_file"], port=value["port"],
                      ready_url=value.get("ready_url", ""))
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", service.name):
            raise InstallError("Invalid plugin service name")
        if not service.command or not Path(service.command[0]).is_absolute():
            raise InstallError("Service executable must be absolute")
        if any(not isinstance(arg, str) or any(c in arg for c in "\n\r\0") for arg in service.command):
            raise InstallError("Invalid service arguments")
        for path in (service.cwd, service.environment_file):
            p = Path(path)
            if not p.is_absolute() or not p.resolve().is_relative_to(root):
                raise InstallError("Service paths must remain inside their plugin installation")
        if type(service.port) is not int or not 1 <= service.port <= 65535:
            raise InstallError("Invalid service port")
        if service.ready_url:
            url = urlsplit(service.ready_url)
            if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.port != service.port
                    or url.username or url.password):
                raise InstallError("Readiness URL must identify the service loopback listener")
        return service
