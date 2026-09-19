"""Short-lived systemd GC trigger. Dispatch still enters the ordinary Harness."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from chatcopilot.harness.config import safe_error
from chatcopilot.harness.models import HarnessError, RepairFeedback
from chatcopilot.harness.schedule_repository import ScheduleRepository
from chatcopilot.harness.models import GovernanceSchedule


def _quote(value):
    return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class GovernanceScheduler:
    def __init__(self, controller, *, unit_directory=None, command=subprocess.run, clock=time.time):
        self.controller, self.command, self.clock = controller, command, clock
        self.store = ScheduleRepository(controller.store.root)
        self.units = Path(unit_directory) if unit_directory else Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
        identity = hashlib.sha256(str(controller.store.root).encode()).hexdigest()[:16]
        self.unit = "agentstrata-harness-gc-" + identity

    def _systemctl(self, *args, required=True):
        result = self.command(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)
        if required and result.returncode:
            raise HarnessError("schedule_unavailable", "systemd 熵回收调度操作失败：" + safe_error(Exception(result.stderr)))
        return result

    def get(self):
        value = self.store.read()
        try:
            result = self._systemctl("show", self.unit + ".timer",
                "--property=LoadState,ActiveState,NextElapseUSecRealtime,NextElapseUSecMonotonic", required=False)
            timer = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if not {"LoadState", "ActiveState"}.issubset(timer):
                timer = {"state": "unknown"}
        except (OSError, subprocess.SubprocessError):
            timer = {"state": "unknown"}
        return {**value, "unit": self.unit, "timer": timer}

    def configure(self, settings: GovernanceSchedule):
        with self.store.locked():
            previous = self.store.read()
            value = {**settings.to_payload(), "revision": uuid.uuid4().hex, "last_run": previous.get("last_run")}
            # A failed disable still prevents a pending timer from creating new work.
            self.store.write(value)
            try:
                if settings.enabled:
                    for parent in (self.units, *self.units.parents):
                        if parent.is_symlink():
                            raise HarnessError("schedule_path", "systemd 配置目录不能含符号链接")
                    self.units.mkdir(parents=True, exist_ok=True)
                    repo = self.controller.repository
                    command = " ".join(_quote(part) for part in (
                        sys.executable, "-m", "chatcopilot.harness", "--repository-root", repo,
                        "--root", self.controller.store.root, "gc-tick"))
                    environment = "Environment=" + _quote("PYTHONPATH=" + str(repo / "src")) + "\n"
                    if os.environ.get("CHATCOPILOT_HARNESS_ENV"):
                        environment += "Environment=" + _quote("CHATCOPILOT_HARNESS_ENV=" + os.environ["CHATCOPILOT_HARNESS_ENV"]) + "\n"
                    service = ("# AgentStrata managed GC trigger\n[Unit]\nDescription=AgentStrata repository GC trigger\n"
                        "[Service]\nType=oneshot\nTimeoutStartSec=0\nUMask=0077\nWorkingDirectory=" + str(repo) + "\n" + environment +
                        "ExecStart=" + command + "\n")
                    timer = ("# AgentStrata managed GC trigger\n[Unit]\nDescription=Periodic AgentStrata repository GC\n"
                        "[Timer]\nOnActiveSec=1min\nOnUnitActiveSec=" + str(settings.interval_hours) + "h\n"
                        "Unit=" + self.unit + ".service\n[Install]\nWantedBy=timers.target\n")
                    for suffix, content in ((".service", service), (".timer", timer)):
                        path = self.units / (self.unit + suffix)
                        if path.is_symlink() or path.exists() and not path.read_text().startswith("# AgentStrata managed GC trigger\n"):
                            raise HarnessError("schedule_path", "拒绝覆盖非本项目管理的 systemd unit")
                        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "w") as stream:
                            stream.write(content)
                        temporary.replace(path)
                    self._systemctl("daemon-reload")
                    self._systemctl("enable", "--now", self.unit + ".timer")
                    self._systemctl("restart", self.unit + ".timer")
                elif (self.units / (self.unit + ".timer")).exists():
                    self._systemctl("disable", "--now", self.unit + ".timer")
            except Exception as exc:
                self.store.write({**value, "enabled": False, "last_error": safe_error(exc)})
                raise

        return self.get()

    def tick(self):
        with self.store.locked():
            value = self.store.read()
            settings = GovernanceSchedule.from_payload(value)
            if not settings.enabled:
                return {"status": "disabled"}
            now = self.clock()
            slot = int(now // (settings.interval_hours * 3600))
            request_id = f"gc-{value['revision']}-{slot}"
            previous = value.get("last_run") or {}
            if previous.get("request_id") == request_id and previous.get("status") in {"created", "skipped"}:
                return {**previous, "duplicate": True}
            active = self.controller.store.active_governance(str(self.controller.repository))
            if active:
                result = {"status": "skipped", "reason": "governance_active", "task_id": active}
            else:
                pending = {"request_id": request_id, "at": now, "status": "dispatching"}
                self.store.write({**value, "last_run": pending})
                try:
                    task = self.controller.start_code_health(settings.options, request_id=request_id,
                        feedback=RepairFeedback(settings.repair_hint))
                    result = {"status": "created", "task_id": task["task_id"]}
                except Exception as exc:
                    self.store.write({**value, "last_run": {**pending, "status": "failed", "message": safe_error(exc)}})
                    raise
            result = {**result, "request_id": request_id, "at": now}
            self.store.write({**value, "last_run": result})
            return result
