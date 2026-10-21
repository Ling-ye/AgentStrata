"""One private installation record per plugin, with an operation lock."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat

import yaml

from chatcopilot.core.file_integrity import require_regular_file
from chatcopilot.core.install_artifacts import InstallError, private_directory, write_private
from chatcopilot.core.settings import parse_local_env_text
from chatcopilot.plugins.types import ObservableAgentDefinition


def _private_directory(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InstallError("Agent registration directories must be private and owner-owned")


def _read_private(root: Path, relative: Path) -> str:
    """Read config/record files through the installation's private directory descriptors."""
    if root.resolve() != root:
        raise InstallError("Installation directory must not contain symlinks")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        _private_directory(os.fstat(descriptor))
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _private_directory(os.fstat(descriptor))
        file = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            require_regular_file(info, owner_uid=os.getuid(), single_link=True)
            if info.st_mode & 0o077:
                raise InstallError("Agent registration files must be private")
            return stream.read()
    finally:
        os.close(descriptor)


def validate_observable_agents(values: list, *, root: Path) -> list[ObservableAgentDefinition]:
    """Validate references without creating a Gateway or copying configuration values."""
    if not isinstance(values, list):
        raise InstallError("observable_agents must be a list")
    agents = [ObservableAgentDefinition.parse(value) for value in values]
    if len({agent.id for agent in agents}) != len(agents):
        raise InstallError("Observable Agent ids must be unique within a plugin")
    for agent in agents:
        try:
            config = yaml.safe_load(_read_private(root, Path(agent.bot_spec)))
        except yaml.YAMLError:
            raise InstallError("Observable Agent BotSpec is invalid YAML") from None
        gateway = config.get("gateway") if isinstance(config, dict) else None
        if not isinstance(gateway, dict):
            raise InstallError("Observable Agents must provide a Gateway BotSpec")
        environment = parse_local_env_text(_read_private(root, Path(agent.env_file)), source="plugin environment")
        key = gateway.get("state_root_env", "CHATCOPILOT_GATEWAY_STATE_ROOT")
        value = environment.get(key, "") if isinstance(key, str) else ""
        state = Path(value)
        if not value or not state.is_absolute() or not state.is_relative_to(root) or state.resolve() != state:
            raise InstallError("Observable Agent state must remain inside its plugin installation")
        current = root
        _private_directory(current.lstat())
        for part in state.relative_to(root).parts:
            current /= part
            if not current.exists() and not current.is_symlink():
                break  # A reader must not create a state/observation directory.
            _private_directory(current.lstat())
    return agents


class InstallRepository:
    def __init__(self, root: Path, plugin_id: str):
        self.root = root.expanduser().absolute() / plugin_id
        self.path = self.root / "installation.json"
        self.plugin_id = plugin_id

    def read(self) -> dict | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        data = json.loads(_read_private(self.root, Path("installation.json")))
        if not isinstance(data, dict):
            raise InstallError("Installation record must be an object")
        if data.get("plugin_id") != self.plugin_id or data.get("root") != str(self.root):
            raise InstallError("Installation record identity does not match its directory")
        return data

    def save(self, data: dict) -> None:
        write_private(self.path, json.dumps({**data, "plugin_id": self.plugin_id,
            "root": str(self.root)}, ensure_ascii=False, indent=2) + "\n")

    @contextmanager
    def lock(self):
        if (self.root.exists() and not self.path.exists()
                and any(p.name != ".lock" for p in self.root.iterdir())):
            raise InstallError("Refusing to adopt an unregistered non-empty directory")
        private_directory(self.root)
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_nlink != 1:
                raise InstallError("Invalid installation lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstallError("Another plugin operation is running") from None
            yield
        finally:
            os.close(fd)
