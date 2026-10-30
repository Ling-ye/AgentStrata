"""Validate installed service declarations and read their private environment."""
from pathlib import Path
import os
import re
from urllib.parse import urlsplit

from chatcopilot.contracts.deployment import ServiceDefinition
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.private_sqlite import private_file
from chatcopilot.core.settings import load_local_env_values


def parse_service_definition(value: dict, *, root: Path) -> ServiceDefinition:
    service = ServiceDefinition(name=value["name"], command=tuple(value["command"]), cwd=value["cwd"],
                  environment_file=value["environment_file"], port=value["port"],
                  ready_url=value.get("ready_url", ""), versioned=value.get("versioned", False))
    if type(service.versioned) is not bool:
        raise InstallError("Service versioned capability must be boolean")
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


def runtime_environment(path: Path) -> dict:
    private_file(path)
    return {**os.environ, **load_local_env_values(path, expand_home=True)}
