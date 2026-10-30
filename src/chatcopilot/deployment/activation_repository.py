"""Private per-installation version artifacts and one recoverable switch intent."""
import json
from pathlib import Path

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory, private_lock
from chatcopilot.core.release_identity import read_json


class ActivationRepository:
    def __init__(self, root: Path):
        self.root = root

    def lock(self):
        private_directory(self.root)
        return private_lock(self.root / "operation.lock")

    def read(self):
        path = self.root / "operation.json"
        if not path.exists() and not path.is_symlink():
            return None
        value = read_json(path)
        if value.get("schema") != "agentstrata-activation/v1":
            raise ValueError("Unsupported activation record")
        return value

    def save(self, value):
        write_private(self.root / "operation.json", json.dumps({**value, "schema": "agentstrata-activation/v1"}, sort_keys=True))

    def archive(self, value):
        write_private(self.root / "history" / (value["id"] + ".json"), json.dumps(value, sort_keys=True))

    def pointer(self):
        path = self.root / "current.json"
        return read_json(path) if path.exists() or path.is_symlink() else None

    def select(self, value):
        path = self.root / "current.json"
        if value is None:
            if path.is_symlink():
                raise ValueError("Version pointer was replaced")
            path.unlink(missing_ok=True)
        else:
            write_private(path, json.dumps(value, sort_keys=True))

    def cancel(self, operation_id):
        write_private(self.root / "cancel.json", json.dumps({"id": operation_id}))

    def cancelled(self, operation_id):
        path = self.root / "cancel.json"
        return (path.exists() or path.is_symlink()) and read_json(path).get("id") == operation_id
