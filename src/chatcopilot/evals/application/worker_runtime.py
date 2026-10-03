"""Local subprocess mechanisms; Evaluation service owns business state and claims."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Sequence

from chatcopilot.evals.application.worker_types import (
    PreparedWorker, WorkerExitCallback, WorkerLaunchRequest, WorkerPidStatus, WorkerProcess,
)


class _PreparedSubprocess:
    def __init__(self, process: subprocess.Popen[Any], gate: int) -> None:
        self.process = process
        self._gate: int | None = gate

    def release(self) -> None:
        if self._gate is None:
            raise RuntimeError("Evaluation worker startup gate is closed")
        try:
            if os.write(self._gate, b"\x01") != 1:
                raise RuntimeError("Evaluation worker startup handshake failed")
        finally:
            self.close()

    def close(self) -> None:
        if self._gate is not None:
            gate, self._gate = self._gate, None
            os.close(gate)


class LocalEvaluationWorker:
    def prepare(self, request: WorkerLaunchRequest) -> PreparedWorker:
        reader, writer = os.pipe()
        process = None
        try:
            os.set_inheritable(reader, True)
            options = {"close_fds": False} if os.name == "nt" else {"pass_fds": (reader,)}
            process = subprocess.Popen(
                [*request.command, "--startup-fd", str(reader)],
                cwd=str(request.cwd), env=dict(request.environment),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=False,
                start_new_session=os.name != "nt",
                creationflags=int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0,
                **options,
            )
        finally:
            os.close(reader)
            if process is None:
                os.close(writer)
        return _PreparedSubprocess(process, writer)

    @staticmethod
    def _wait_owned(process: WorkerProcess, on_exit: WorkerExitCallback) -> None:
        on_exit(process.wait())

    def watch(self, process: WorkerProcess, on_exit: WorkerExitCallback) -> None:
        threading.Thread(target=self._wait_owned, args=(process, on_exit),
                         name=f"evaluation-{process.pid}", daemon=True).start()

    def _wait_inherited(self, pid: int, directory: Path, on_exit: WorkerExitCallback) -> None:
        while self.observe(pid, directory) != "exited":
            time.sleep(0.1)
        on_exit(None)

    def watch_inherited(self, pid: int, directory: Path, on_exit: WorkerExitCallback) -> None:
        threading.Thread(target=self._wait_inherited, args=(pid, directory, on_exit),
                         name=f"evaluation-inherited-{directory.name}", daemon=True).start()


    @classmethod
    def discover(cls, directory: Path) -> Sequence[int]:
        """Find same-user managed workers when startup PID persistence was interrupted."""

        if os.name == "nt":
            return []
        proc = Path("/proc")
        try:
            entries = tuple(proc.iterdir())
        except OSError:
            return []
        matches: list[int] = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid <= 0 or pid == os.getpid():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            if cls._pid_matches_evaluation(pid, directory):
                matches.append(pid)
        return sorted(matches)


    @staticmethod
    def _pid_matches_evaluation(pid: int, directory: Path) -> bool:
        if pid <= 0:
            return False
        argv: Sequence[str]
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"(Get-CimInstance Win32_Process -Filter "
                            f'"ProcessId = {pid}").CommandLine'
                        ),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            argv = LocalEvaluationWorker._split_windows_command_line(completed.stdout.strip())
        else:
            try:
                raw_argv = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                return False
            argv = [
                value.decode("utf-8", errors="replace") for value in raw_argv.split(b"\0") if value
            ]
        return LocalEvaluationWorker._argv_matches_evaluation(argv, directory)


    @staticmethod
    def _argv_matches_evaluation(
        argv: Sequence[str],
        directory: Path,
    ) -> bool:
        normalized_tokens = [str(value).casefold() for value in argv]
        managed_entries = [
            index
            for index, value in enumerate(normalized_tokens)
            if value == "chatcopilot.evals.managed_worker"
            and index > 0
            and normalized_tokens[index - 1] == "-m"
        ]
        if len(managed_entries) != 1:
            return False
        output_values: list[str] = []
        index = 0
        while index < len(argv):
            value = str(argv[index])
            if value == "--output":
                if index + 1 >= len(argv):
                    return False
                output_values.append(str(argv[index + 1]))
                index += 2
                continue
            if value.startswith("--output="):
                output_values.append(value.partition("=")[2])
            index += 1
        if len(output_values) != 1:
            return False
        output = Path(output_values[0])
        if not output.is_absolute():
            return False
        try:
            actual = os.path.normcase(str(output.resolve(strict=False)))
            expected = os.path.normcase(str(directory.resolve(strict=False)))
        except OSError:
            return False
        return actual == expected


    @staticmethod
    def _split_windows_command_line(command_line: str) -> Sequence[str]:
        argv: list[str] = []
        length = len(command_line)
        index = 0
        while index < length:
            while index < length and command_line[index] in " \t":
                index += 1
            if index >= length:
                break
            value: list[str] = []
            quoted = False
            while index < length:
                char = command_line[index]
                if char in " \t" and not quoted:
                    break
                if char == "\\":
                    start = index
                    while index < length and command_line[index] == "\\":
                        index += 1
                    slash_count = index - start
                    if index < length and command_line[index] == '"':
                        value.extend("\\" * (slash_count // 2))
                        if slash_count % 2:
                            value.append('"')
                            index += 1
                        elif quoted and index + 1 < length and command_line[index + 1] == '"':
                            value.append('"')
                            index += 2
                        else:
                            quoted = not quoted
                            index += 1
                        continue
                    value.extend("\\" * slash_count)
                    continue
                if char == '"':
                    if quoted and index + 1 < length and command_line[index + 1] == '"':
                        value.append('"')
                        index += 2
                    else:
                        quoted = not quoted
                        index += 1
                    continue
                value.append(char)
                index += 1
            argv.append("".join(value))
            while index < length and command_line[index] in " \t":
                index += 1
        return argv


    @classmethod
    def observe(
        cls,
        pid: int,
        directory: Path,
    ) -> WorkerPidStatus:
        if cls._pid_matches_evaluation(pid, directory):
            return "matched"
        if cls._pid_is_zombie(pid):
            return "exited"
        if cls.exists(pid):
            return "unknown"
        return "exited"


    @staticmethod
    def _pid_is_zombie(pid: int) -> bool:
        if os.name == "nt" or pid <= 0:
            return False
        try:
            value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        close = value.rfind(")")
        return close >= 0 and value[close + 2 : close + 3] == "Z"


    @staticmethod
    def exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"$p = Get-Process -Id {pid} "
                            "-ErrorAction SilentlyContinue; "
                            "if ($null -eq $p) { 'missing' } "
                            "else { 'present' }"
                        ),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return True
            observation = completed.stdout.strip().lower()
            if observation == "missing":
                return False
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True


    @staticmethod
    def request_stop(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        try:
            # Cooperative cancellation targets only the managed Core.  It owns
            # the active Trial supervisor and must let that subreaper prove all
            # descendants are gone.  Signalling the worker's whole session can
            # kill a just-spawned supervisor before its cleanup-ready handshake.
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return


    @staticmethod
    def force_stop(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            return


    @staticmethod
    def abort(process: WorkerProcess) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0 and process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("evaluation process did not stop after termination") from exc
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("evaluation process did not stop after termination") from exc
        except ProcessLookupError:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("evaluation process identity could not be confirmed") from exc

