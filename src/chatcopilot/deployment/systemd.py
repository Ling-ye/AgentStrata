"""Common local service operations; installation-specific ownership stays with the caller."""
from __future__ import annotations

import platform
import socket
import subprocess
import sys
from urllib.request import urlopen

from chatcopilot.contracts.deployment import ServiceDefinition
from chatcopilot.core.install_artifacts import InstallError


def require_platform() -> None:
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise InstallError("Managed software plugins currently support Linux/WSL x86_64")

def require_free_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise InstallError("Port must be between 1 and 65535")
    try:
        with socket.socket() as probe:
            # A stopped HTTP service can leave accepted connections in TIME_WAIT.
            # Match the servers' reuse semantics; a live listener still rejects bind.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise InstallError(f"Loopback port {port} is occupied; select another port explicitly") from None


class UserSystemd:
    def command(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=60)

    def preflight(self) -> None:
        result = self.command("show-environment")
        if result.returncode:
            raise InstallError("systemd user manager is unavailable; enable WSL/Linux user services first")

    @staticmethod
    def ready(service: ServiceDefinition) -> bool:
        try:
            if service.ready_url:
                with urlopen(service.ready_url, timeout=2) as result:
                    return result.status == 200
            with socket.create_connection(("127.0.0.1", service.port), timeout=1):
                return True
        except OSError:
            return False
