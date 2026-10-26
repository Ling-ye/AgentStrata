"""Executable, artifact and systemd adapters for the plugin control plane."""
from __future__ import annotations

from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from chatcopilot.core.install_artifacts import (
    InstallError, dependency_environment, download, extract_archive, private_directory,
    run_install, sha256, verify_wheel,
)
from chatcopilot.plugins.types import PluginDefinition, ServiceDefinition
from chatcopilot.project import PROJECT_REPOSITORY


UV_VERSION = "0.12.5"
UV_ARCHIVE_SHA256 = "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2"
UV_BINARY_SHA256 = "b65f23a420c4acc96427efb30e5ed9bc0f7e25d2d712000f6ede77c1a0de5f46"


def require_platform() -> None:
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise InstallError("Managed software plugins currently support Linux/WSL x86_64")


def release_artifacts(entry: PluginDefinition, cache: Path, local: Path | None) -> tuple[Path, Path, str]:
    host_version = version("agentstrata")
    names = (f"agentstrata-{host_version}-py3-none-any.whl", entry.wheel)
    private_directory(cache)
    base = f"{PROJECT_REPOSITORY}/releases/download/v{host_version}"
    if local is None:
        if not re.fullmatch(r"\d+\.\d+\.\d+", host_version):
            raise InstallError("Development builds need --artifacts containing both wheels and SHA256SUMS")
        try:
            with urlopen(base + "/SHA256SUMS", timeout=30) as response:
                sums = response.read(1024 * 1024).decode()
        except Exception as exc:
            raise InstallError(f"Published release checksums unavailable: {type(exc).__name__}") from None
    else:
        sums = (local / "SHA256SUMS").read_text()
    hashes = {}
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2:
            name = parts[1].removeprefix("*")
            if name in hashes:
                raise InstallError("Duplicate release checksum entry")
            hashes[name] = parts[0]
    paths = []
    for name, package, expected_version in zip(names, ("agentstrata", entry.package),
                                                (host_version, entry.version), strict=True):
        digest = hashes.get(name, "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise InstallError(f"Missing SHA256 for release artifact: {name}")
        target = cache / name
        if local is not None:
            original = local / name
            if original.is_symlink() or not original.is_file() or sha256(original) != digest:
                raise InstallError(f"Release checksum mismatch: {name}")
            shutil.copyfile(original, target)
        else:
            shutil.copyfile(download(base + "/" + name, digest, cache), target)
        if sha256(target) != digest:
            raise InstallError(f"Release artifact changed: {name}")
        verify_wheel(target, name=package, version=expected_version)
        paths.append(target)
    source = f"{PROJECT_REPOSITORY}/archive/refs/tags/v{host_version}.tar.gz" if local is None else ""
    return paths[0], paths[1], source


def ensure_uv(root: Path) -> Path:
    existing = Path.home() / ".local/share/agentstrata/uv" / UV_VERSION / "bin/uv"
    if existing.is_file() and sha256(existing) == UV_BINARY_SHA256:
        return existing
    target = root / "tools/uv" / UV_VERSION
    binary = target / "uv"
    if binary.is_file() and sha256(binary) == UV_BINARY_SHA256:
        return binary
    archive = download(f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz",
                       UV_ARCHIVE_SHA256, root / "cache")
    if target.exists():
        shutil.rmtree(target)
    extract_archive(archive, target)
    if sha256(binary) != UV_BINARY_SHA256:
        raise InstallError("uv executable checksum mismatch")
    return binary


def bootstrap(entry: PluginDefinition, root: Path, artifacts: tuple[Path, Path], uv: Path) -> Path:
    python = root / "runtime/venv/bin/python"
    env = dependency_environment()
    env["UV_PYTHON_INSTALL_DIR"] = str(root.parent / "toolchains/python")
    log = root / "logs/bootstrap.log"
    if not python.exists():
        run_install([str(uv), "venv", "--python", entry.python, "--managed-python", "--no-config",
                     str(python.parent.parent)], log=log, env=env)
    run_install([str(uv), "pip", "install", "--python", str(python), "--no-config", "--no-deps", "--reinstall",
                 *map(str, artifacts)], log=log, env=env)
    return python


def call_driver(entry: PluginDefinition, root: Path, operation: str) -> dict:
    command = [str(root / "runtime/venv/bin/python"), "-m", entry.module, operation,
               "--request", str(root / "config/request.json")]
    log = root / "logs" / f"{operation}.log"
    private_directory(log.parent)
    with log.open("a", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=root, env=dependency_environment(),
                                stdout=subprocess.PIPE, stderr=output, text=True, check=False)
    if result.returncode:
        raise InstallError(f"Plugin {operation} failed (exit {result.returncode}); see {log}")
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise InstallError(f"Invalid plugin {operation} response; see {log}") from None


def _systemd_value(value: str) -> str:
    if any(c in value for c in "\n\r\0"):
        raise InstallError("Invalid systemd argument")
    return value.replace("%", "%%")


def _systemd_quote(value: str, *, executable_argument: bool = False) -> str:
    value = _systemd_value(value)
    if executable_argument:
        value = value.replace("$", "$$")
    return json.dumps(value, ensure_ascii=False)


class UserServices:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"

    @staticmethod
    def name(plugin_id: str, service: ServiceDefinition) -> str:
        return f"agentstrata-plugin-{plugin_id}-{service.name}.service"

    def command(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=60)

    def preflight(self) -> None:
        result = self.command("show-environment")
        if result.returncode:
            raise InstallError("systemd user manager is unavailable; enable WSL/Linux user services first")

    def active(self, plugin_id: str, service: ServiceDefinition) -> bool:
        return self.command("is-active", "--quiet", self.name(plugin_id, service)).returncode == 0

    @staticmethod
    def version_command(service: ServiceDefinition, root: Path) -> tuple[str, ...]:
        command = service.command
        if not service.versioned:
            return command
        if len(command) < 3 or command[1] != "-m":
            raise InstallError("Managed plugin versions require an explicit Python module service")
        wrapped = [command[0], "-m", "chatcopilot.core.release_runtime", "--control", str(root / ".agentstrata-versions"),
                   "--service", service.name, "--component", "chatcopilot"]
        package = command[2].split(".")[0]
        if package != "chatcopilot":
            wrapped += ["--component", package]
        if "--bot" in command:
            wrapped += ["--bot", command[command.index("--bot") + 1]]
        return tuple([*wrapped, "--", *command])

    def _owned(self, plugin_id: str, services: list[ServiceDefinition], root: Path,
               *, missing_ok: bool = False) -> None:
        owner = f"# AgentStrata plugin installation: {root}\n"
        for service in services:
            path = self.directory / self.name(plugin_id, service)
            if not path.exists() and not path.is_symlink() and missing_ok:
                state = self.command("show", self.name(plugin_id, service),
                                     "--property=LoadState", "--value").stdout.strip()
                if state in {"", "not-found"}:
                    continue
            if path.is_symlink() or not path.is_file() or not path.read_text().startswith(owner):
                raise InstallError("Refusing to control a service owned by another installation")

    def register(self, plugin_id: str, services: list[ServiceDefinition], root: Path) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._owned(plugin_id, services, root, missing_ok=True)
        owner = f"# AgentStrata plugin installation: {root}\n"
        for service in services:
            path = self.directory / self.name(plugin_id, service)
            if path.exists() and (path.is_symlink() or not path.read_text().startswith(owner)):
                raise InstallError("Refusing to replace a service owned by another installation")
            content = owner + "[Unit]\nDescription=AgentStrata optional software\nAfter=default.target\n\n[Service]\nType=simple\n"
            # Path directives consume a whole path, not an ExecStart-style quoted word.
            content += "WorkingDirectory=" + _systemd_value(service.cwd) + "\n"
            content += "EnvironmentFile=" + _systemd_value(service.environment_file) + "\n"
            content += "ExecStart=" + " ".join(_systemd_quote(arg, executable_argument=True) for arg in self.version_command(service, root)) + "\n"
            content += "Restart=on-failure\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=30\nUMask=0077\n"
            # Unit files contain references only; the referenced credential file is private.
            path.write_text(content)
        if self.command("daemon-reload").returncode:
            raise InstallError("systemd daemon-reload failed")

    def start(self, plugin_id: str, services: list[ServiceDefinition], root: Path) -> None:
        self._owned(plugin_id, services, root)
        started = []
        try:
            for service in services:
                if self.active(plugin_id, service):
                    continue
                require_free_port(service.port)
                name = self.name(plugin_id, service)
                started.append(service)
                if self.command("start", name).returncode:
                    raise InstallError(f"Service failed to start: {name}")
                deadline = time.monotonic() + 60
                while not self._ready(service):
                    if self.command("is-failed", "--quiet", name).returncode == 0 or time.monotonic() >= deadline:
                        raise InstallError(f"Service did not become ready: {name}; inspect journalctl --user -u {name}")
                    time.sleep(0.25)
        except BaseException as error:
            try:
                self.stop(plugin_id, started, root)
            except InstallError as cleanup:
                raise InstallError(f"{error}; cleanup also failed: {cleanup}") from None
            raise

    @staticmethod
    def _ready(service: ServiceDefinition) -> bool:
        try:
            if service.ready_url:
                with urlopen(service.ready_url, timeout=2) as result:
                    return result.status == 200
            with socket.create_connection(("127.0.0.1", service.port), timeout=1):
                return True
        except OSError:
            return False

    def stop(self, plugin_id: str, services: list[ServiceDefinition], root: Path) -> None:
        self._owned(plugin_id, services, root, missing_ok=True)
        for service in reversed(services):
            name = self.name(plugin_id, service)
            if self.command("show", name, "--property=LoadState", "--value").stdout.strip() == "not-found":
                continue
            if self.command("stop", name).returncode:
                raise InstallError(f"Service could not be stopped: {name}")

    def remove(self, plugin_id: str, services: list[ServiceDefinition], root: Path) -> None:
        self.stop(plugin_id, services, root)
        owner = f"# AgentStrata plugin installation: {root}\n"
        for service in services:
            path = self.directory / self.name(plugin_id, service)
            if path.exists():
                if path.is_symlink() or not path.read_text().startswith(owner):
                    raise InstallError("Refusing to remove a service owned by another installation")
                path.unlink()
        if self.command("daemon-reload").returncode:
            raise InstallError("systemd daemon-reload failed")


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


def create_manager(root: Path):
    from chatcopilot.plugins.catalog import PLUGINS
    from chatcopilot.plugins.service import PluginManager

    return PluginManager(root, PLUGINS, services=UserServices(), driver=call_driver,
                         bootstrap=bootstrap, artifacts=release_artifacts, toolchain=ensure_uv)
