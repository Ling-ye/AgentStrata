"""Systemd and process facts for the Harness control service."""
from __future__ import annotations

import fcntl
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from chatcopilot.core.private_sqlite import private_directory, private_file, private_lock
from chatcopilot.core.source_snapshot import copy_sources, source_manifest
from chatcopilot.harness.control_types import DispatchResult, WorkerState
from chatcopilot.harness.models import ACTIVE, HarnessError, PIPELINE_VERSION
from chatcopilot.harness.store import HarnessStore


def _worker_directory(root: Path, task_id: str) -> Path:
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        raise ValueError("invalid Harness task path")
    directory = root / "jobs" / task_id
    if directory.resolve() != directory:
        raise ValueError("Harness task directory contains a symlink")
    return directory


@contextmanager
def worker_execution(store: HarnessStore, task_id: str, *, delivery: bool = False) -> Iterator[dict[str, Any] | None]:
    """Register execution under the control lock, then keep only execution/maintenance locks."""
    with ExitStack() as lifetime:
        lifetime.enter_context(store.creation_guard())
        with store.control_guard(task_id, wait=True):
            task = store.get(task_id)
            if task.get("pipeline_version") != PIPELINE_VERSION or (
                    task["source"].get("kind") == "code_health" and not task.get("governance_run_id")):
                raise HarnessError("source_archived", "旧任务只读保留，请创建新任务")
            eligible = (bool(task.get("delivery")) and task["status"] not in {*ACTIVE, "waiting_input"}
                        and not task.get("current_evaluation_id")) if delivery else (
                task["status"] in {"queued", "running"} and not task.get("delivery_evaluation"))
            if eligible:
                if delivery:
                    from chatcopilot.harness.delivery_archive import task_paths
                    _, directory, _, _ = task_paths(store, task)
                else:
                    directory = _worker_directory(store.root, task_id)
                try:
                    lifetime.enter_context(private_lock(directory / "worker.lock"))
                except BlockingIOError as exc:
                    raise HarnessError("worker_active", "本任务已有 worker 执行") from exc
        yield task if eligible else None


class SystemdWorkerControl:
    def __init__(self, repository: Path, root: Path, settings: Mapping[str, str]) -> None:
        self.repository, self.root = repository, root
        environment = {name: os.environ[name] for name in ("PATH", "LANG", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "NO_PROXY", "CHATCOPILOT_CODEX_BIN", "CHATCOPILOT_CODEX_BOT_HOME",
            "CHATCOPILOT_EVALUATION_SOCKET") if os.environ.get(name)}
        self.settings = MappingProxyType({**environment, **settings})

    @staticmethod
    def delivery_unit(task: dict[str, Any]) -> str:
        return "agentstrata-harness-delivery-" + task["task_id"][7:]

    def _directory(self, task: dict[str, Any]) -> Path:
        return _worker_directory(self.root, task["task_id"])

    @staticmethod
    def _unit_state(unit: str) -> WorkerState:
        result = subprocess.run(["systemctl", "--user", "show", unit,
            "--property=LoadState,ActiveState,Job"], capture_output=True, text=True, timeout=5)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if not {"LoadState", "ActiveState", "Job"}.issubset(fields):
            return WorkerState.UNKNOWN
        if result.returncode and not (result.returncode == 4 and fields.get("LoadState") == "not-found"):
            return WorkerState.UNKNOWN
        if fields.get("Job", "").strip() not in {"", "0"}:
            return WorkerState.ACTIVE
        state = fields.get("ActiveState")
        if state in {"active", "activating", "deactivating", "reloading", "refreshing"}:
            return WorkerState.ACTIVE
        if state in {"inactive", "failed"} and fields.get("LoadState") in {"loaded", "not-found"}:
            return WorkerState.INACTIVE
        return WorkerState.UNKNOWN

    def observe(self, task: dict[str, Any]) -> WorkerState:
        descriptor = None
        try:
            path = self._directory(task) / "worker.lock"
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            except FileNotFoundError:
                pass
            if descriptor is not None:
                current, opened = private_file(path), os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    return WorkerState.UNKNOWN
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return WorkerState.ACTIVE
            states = (self._unit_state(task["unit"]), self._unit_state(self.delivery_unit(task)))
            if WorkerState.ACTIVE in states:
                return WorkerState.ACTIVE
            return WorkerState.UNKNOWN if WorkerState.UNKNOWN in states else WorkerState.INACTIVE
        except (OSError, ValueError, subprocess.SubprocessError):
            return WorkerState.UNKNOWN
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def launch(self, task: dict[str, Any]) -> DispatchResult:
        return self._launch(task, delivery=False)

    def launch_delivery(self, task: dict[str, Any]) -> DispatchResult:
        return self._launch(task, delivery=True)

    def _launch(self, task: dict[str, Any], *, delivery: bool) -> DispatchResult:
        try:
            if not shutil.which("systemd-run"):
                return DispatchResult("failed", "worker_unavailable", "后台 Harness 需要可用的 systemd 用户服务")
            directory = private_directory(self._directory(task))
            runtime = directory / "runtime"
            if not runtime.exists():
                copy_sources(self.repository, runtime, source_manifest(self.repository))
            module = "delivery_runtime" if delivery else "worker"
            if runtime.resolve() != runtime or not (runtime / f"src/chatcopilot/harness/{module}.py").is_file():
                return DispatchResult("failed", "worker_runtime_missing", "冻结的 Harness 宿主不可用")
            unit = self.delivery_unit(task) if delivery else task["unit"]
            command = ["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
                "--property=Type=exec", "--property=KillMode=control-group", "--property=UMask=0077",
                "--property=WorkingDirectory=" + str(runtime)]
            if not delivery:
                command.extend(("--property=MemoryMax=3G", "--property=TasksMax=256"))
            environment = {**self.settings, "PYTHONPATH": str(runtime / "src")}
            command.extend("--setenv=" + key + "=" + value for key, value in environment.items())
            command.extend([sys.executable, "-m", "chatcopilot.harness." + module,
                "--root", str(self.root), "--task", task["task_id"]])
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return DispatchResult("scheduled")
        except subprocess.TimeoutExpired:
            return DispatchResult("unknown", "worker_unavailable", "调度结果暂时无法确认，等待生命周期核对")
        except (OSError, ValueError, subprocess.SubprocessError):
            return DispatchResult("failed", "worker_unavailable", "Harness 宿主准备或调度失败")
        state = self.observe(task)
        if state == WorkerState.ACTIVE:
            return DispatchResult("scheduled")
        if state == WorkerState.UNKNOWN:
            return DispatchResult("unknown", "worker_unavailable", "调度结果暂时无法确认，等待生命周期核对")
        return DispatchResult("failed", "worker_unavailable", "systemd 未接受任务，请检查用户服务状态")

    @staticmethod
    def maintenance(command: list[str], descriptor: int) -> int:
        if not command:
            raise ValueError("maintenance requires an explicit command")
        return subprocess.run(command, pass_fds=(descriptor,),
            env={**os.environ, "CHATCOPILOT_HARNESS_MAINTENANCE_HELD": "1"}, check=False).returncode
