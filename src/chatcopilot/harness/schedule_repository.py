"""Private schedule settings and dispatch receipts, using a stable sidecar lock."""
import os
from contextlib import contextmanager
from pathlib import Path
import json
import tempfile

from chatcopilot.core.private_sqlite import private_directory, private_file, private_lock, json_text
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.models import GovernanceSchedule


class ScheduleRepository:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "gc-schedule.json"

    def read(self):
        if not self.path.exists():
            return {**GovernanceSchedule().to_payload(), "revision": "", "last_run": None}
        private_file(self.path)
        value = json.loads(self.path.read_text())
        GovernanceSchedule.from_payload(value)
        return value

    def write(self, value):
        private_directory(self.root)
        fd, name = tempfile.mkstemp(prefix="gc-schedule-", suffix=".tmp", dir=self.root)
        temporary = Path(name)
        with os.fdopen(fd, "w") as stream:
            stream.write(json_text(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.path)

    @contextmanager
    def locked(self):
        try:
            with private_lock(self.root / "gc-schedule.lock"):
                yield
        except BlockingIOError as exc:
            raise HarnessError("schedule_busy", "治理调度正在更新，请稍后重试") from exc
