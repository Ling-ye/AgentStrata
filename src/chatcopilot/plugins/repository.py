"""One private installation record per plugin, with an operation lock."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat

from chatcopilot.core.install_artifacts import InstallError, private_directory, write_private


class InstallRepository:
    def __init__(self, root: Path, plugin_id: str):
        self.root = root.expanduser().absolute() / plugin_id
        self.path = self.root / "installation.json"
        self.plugin_id = plugin_id

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        info = self.path.lstat()
        if (self.root.resolve() != self.root or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077):
            raise InstallError("Installation record must be a private owner-owned file")
        data = json.loads(self.path.read_text())
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
