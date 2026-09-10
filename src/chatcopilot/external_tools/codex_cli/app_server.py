"""One-turn stdio RPC transport; owns the process, never retries a turn."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from chatcopilot.contracts.cancellation import CancellationRequested


class AppServerProcess:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str],
                 timeout_seconds: float, on_notification: Callable[[str, dict], None],
                 on_poll: Callable[[], None]) -> None:
        self.command, self.cwd, self.env = command, cwd, env
        self.deadline = time.monotonic() + timeout_seconds
        self.on_notification, self.on_poll = on_notification, on_poll
        self.inbox: queue.Queue[Any] = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self.serial = 0
        self.thread_id = ""
        self.turn_id = ""
        self.terminal = False
        self.stderr = ""

    def __enter__(self) -> AppServerProcess:
        self.process = subprocess.Popen(self.command, cwd=self.cwd, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._read_errors, daemon=True)]
        for reader in self.readers:
            reader.start()
        return self

    def _put(self, value: Any) -> None:
        while not self.closed.is_set():
            try:
                self.inbox.put(value, timeout=.05)
                return
            except queue.Full:
                continue

    def _read(self) -> None:
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise RuntimeError("App Server protocol record exceeds limit")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("App Server protocol record is not an object")
                self._put(value)
        except (OSError, ValueError, RuntimeError) as exc:
            self._put(exc)
        finally:
            self._put(None)

    def _read_errors(self) -> None:
        while not self.closed.is_set():
            data = self.process.stderr.read(4096)
            if not data:
                return
            self.stderr = (self.stderr + data.decode("utf-8", errors="replace"))[-65536:]

    def send(self, value: dict) -> None:
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            value = self.receive()
            if value is None or value.get("id") != request_id:
                continue
            if "error" in value:
                raise RuntimeError(f"App Server {method} failed: {value['error']}")
            result = value.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"App Server {method} returned an invalid result")
            return result

    def receive(self, *, cancelling: bool = False) -> dict | None:
        if not cancelling:
            self.on_poll()
            if time.monotonic() >= self.deadline:
                raise subprocess.TimeoutExpired(self.command, 0)
        try:
            value = self.inbox.get(timeout=.05)
        except queue.Empty:
            return None
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError("App Server disconnected before completion: " + self.stderr[-4000:])
        method = value.get("method")
        if not isinstance(method, str):
            return value
        params = value.get("params") or {}
        if not isinstance(params, dict):
            raise RuntimeError("Invalid App Server notification parameters")
        if "id" in value:
            self._deny_request(value)
            raise RuntimeError(f"Unexpected App Server interaction rejected: {method}")
        if method == "turn/started" and params.get("threadId") == self.thread_id:
            turn_id = (params.get("turn") or {}).get("id")
            if isinstance(turn_id, str):
                self.turn_id = turn_id
        if method == "turn/completed" and params.get("threadId") == self.thread_id:
            if (params.get("turn") or {}).get("id") == self.turn_id:
                self.terminal = True
        self.on_notification(method, params)
        return None

    def _deny_request(self, value: dict) -> None:
        method = value["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            reply = {"result": {"decision": "decline"}}
        elif method == "item/permissions/requestApproval":
            reply = {"result": {"permissions": {}, "scope": "turn"}}
        elif method == "mcpServer/elicitation/request":
            reply = {"result": {"action": "decline", "content": None}}
        else:
            reply = {"error": {"code": -32601, "message": "Interactive requests are not supported"}}
        self.send({"id": value["id"], **reply})

    def interrupt(self) -> None:
        if not self.turn_id or self.terminal:
            return
        self.serial += 1
        self.send({"id": self.serial, "method": "turn/interrupt",
                   "params": {"threadId": self.thread_id, "turnId": self.turn_id}})
        deadline = time.monotonic() + 2
        while not self.terminal and time.monotonic() < deadline:
            self.receive(cancelling=True)

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type and issubclass(exc_type, (CancellationRequested, subprocess.TimeoutExpired)):
                try:
                    self.interrupt()
                except (OSError, RuntimeError, ValueError):
                    pass
        finally:
            self.closed.set()
            # Descendants must leave before credential copy-back releases the lease.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for reader in self.readers:
                reader.join(timeout=1)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()


def run_app_server(command: list[str], *, cwd: Path, env: dict[str, str], prompt: str,
                   model: str, effort: str, thread_id: str, image_paths: tuple[str, ...],
                   timeout_seconds: float, on_notification: Callable[[str, dict], None],
                   on_thread: Callable[[str], None], on_poll: Callable[[], None]) -> subprocess.CompletedProcess:
    with AppServerProcess(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds,
                          on_notification=on_notification, on_poll=on_poll) as rpc:
        rpc.request("initialize", {"clientInfo": {"name": "agentstrata", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
        rpc.send({"method": "initialized", "params": {}})
        params: dict[str, Any] = {"cwd": str(cwd), "model": model, "approvalPolicy": "never"}
        if thread_id:
            params["threadId"] = thread_id
            # Resume state is provider-owned; replaying its entire history into
            # this adapter would bypass the bounded observation protocol.
            params["excludeTurns"] = True
        result = rpc.request("thread/resume" if thread_id else "thread/start", params)
        native_id = (result.get("thread") or {}).get("id")
        if not isinstance(native_id, str) or not native_id or thread_id and native_id != thread_id:
            raise RuntimeError("App Server thread identity mismatch")
        if result.get("instructionSources"):
            raise RuntimeError("App Server loaded unexpected instruction sources")
        rpc.thread_id = native_id
        on_thread(native_id)
        inputs = [{"type": "text", "text": prompt}]
        inputs.extend({"type": "localImage", "path": path} for path in image_paths)
        turn = rpc.request("turn/start", {"threadId": native_id, "input": inputs,
            "model": model, "effort": effort, "summary": "auto", "approvalPolicy": "never"})
        turn_id = (turn.get("turn") or {}).get("id")
        if not isinstance(turn_id, str) or not turn_id or rpc.turn_id and turn_id != rpc.turn_id:
            raise RuntimeError("App Server turn identity mismatch")
        rpc.turn_id = turn_id
        on_notification("turn/started", {"threadId": native_id, "turn": turn["turn"]})
        while not rpc.terminal:
            rpc.receive()
        return subprocess.CompletedProcess(command, 0, "", rpc.stderr)
