"""Foreground native application lifecycle, without a system service or daemon API."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from chatcopilot.contracts.deployment import ServiceDefinition
from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.application_installation import application_identity, installation
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory, private_file, private_lock
from chatcopilot.core.release_identity import process_identity, owns_listener
from chatcopilot.deployment.component_repository import load_components
from chatcopilot.deployment.configuration import runtime_environment
from chatcopilot.deployment.systemd import UserSystemd, require_free_port


def event_name(root: Path, suffix="stop") -> str:
    digest = hashlib.sha256(os.path.normcase(str(root.absolute())).encode()).hexdigest()
    return "Local\\AgentStrata." + digest + "." + suffix


def service_definitions(root: Path) -> tuple[ServiceDefinition, ...]:
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit native application installation is required")
    bot = root / installed.bot_relative
    spec = load_application_spec(bot)
    environment = runtime_environment(installed.control / "runtime.env")
    gateway = spec.raw["gateway"]
    service = ServiceDefinition("gateway", (str(root / ".venv/Scripts/python.exe"), "-m", "chatcopilot",
        "run", "--bot", str(bot)), str(root), str(installed.control / "runtime.env"),
        int(environment[gateway.get("port_env", "CHATCOPILOT_GATEWAY_PORT")]), versioned=True)
    return (*load_components(root)[0], service)


def observe(root: Path) -> dict:
    identity = application_identity(root)
    if identity is None:
        raise ValueError("No independent application is installed here")
    installed = installation(root)
    result = {**identity, "ok": False, "state": "stopped", "plugins": load_components(root)[3], "services": {}}
    supervisor = installed.control / "supervisor.json"
    if not supervisor.exists():
        return result
    private_file(supervisor)
    record = json.loads(supervisor.read_text())
    if record.get("process") != process_identity(record.get("process", {}).get("pid", 0)):
        return result
    result["state"] = "starting"
    for service in service_definitions(root):
        receipt = installed.control / "running" / (service.name + ".json")
        try:
            private_file(receipt)
            value = json.loads(receipt.read_text())
            pid = value["process"]["pid"]
            loaded = (value.get("loaded") is True and value.get("service") == service.name
                      and value["process"] == process_identity(pid) and owns_listener(pid, service.port)
                      and value.get("version") == identity["source_digest"] and UserSystemd.ready(service))
        except (OSError, ValueError, KeyError, TypeError):
            loaded = False
        result["services"][service.name] = {"loaded": loaded, "version": identity["source_digest"] if loaded else None}
    if result["services"] and all(value["loaded"] for value in result["services"].values()):
        result.update(ok=True, state="ready")
    return result


def stop(root: Path, *, timeout=35) -> dict:
    import win32con
    import win32event
    import pywintypes
    installed = installation(root)
    if installed is None:
        raise ValueError("No independent application is installed here")
    try:
        event = win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, event_name(root))
    except pywintypes.error as error:
        if error.winerror != 2:
            raise
        if observe(root)["state"] != "stopped":
            raise RuntimeError("Native supervisor is alive without its stop event")
    else:
        try:
            win32event.SetEvent(event)
        finally:
            event.Close()
        deadline = time.monotonic() + timeout
        while observe(root)["state"] != "stopped":
            if time.monotonic() >= deadline:
                raise RuntimeError("Native application shutdown is not confirmed")
            time.sleep(0.1)
    return {"ok": True, "state": "stopped", "bot_id": installed.bot_id}


def run(root: Path, *, watch_stdin=False, startup_timeout=120, shutdown_timeout=30) -> int:
    import win32event
    from chatcopilot.core.windows_process import ProcessJob
    from chatcopilot.core.windows_storage import security_attributes
    from chatcopilot.core.process_shutdown import STOP_EVENT

    installed = installation(root)
    if installed is None:
        raise ValueError("No independent application is installed here")
    private_directory(installed.control)
    services = service_definitions(root)
    children, handles, logs = [], [], []
    stopping = threading.Event()
    with private_lock(installed.control / "supervisor.lock", timeout=0):
        job = ProcessJob()
        stop_event = win32event.CreateEvent(security_attributes(), True, False, event_name(root))
        handles.append(stop_event)
        write_private(installed.control / "supervisor.json", json.dumps({"process": process_identity(os.getpid())}))
        def parent_lifetime():
            # EOF also arrives if the native desktop process crashes.
            # Poll the pipe rather than holding BufferedReader's lock during
            # interpreter finalization after a failed service startup.
            import msvcrt
            import pywintypes
            import win32pipe
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            while not stopping.wait(0.1):
                try:
                    _, available, _ = win32pipe.PeekNamedPipe(handle, 0)
                except pywintypes.error as error:
                    if error.winerror in {6, 109, 232}:
                        stopping.set()
                        return
                    raise
                if available:
                    os.read(sys.stdin.fileno(), min(available, 4096))
        if watch_stdin:
            threading.Thread(target=parent_lifetime, daemon=True, name="desktop-lifetime").start()
        previous = {number: signal.signal(number, lambda *_: stopping.set())
                    for number in (signal.SIGINT, signal.SIGTERM)}
        try:
            for service in services:
                require_free_port(service.port)
                event = win32event.CreateEvent(security_attributes(), True, False, event_name(root, service.name))
                handles.append(event)
                environment = runtime_environment(Path(service.environment_file))
                environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                    CHATCOPILOT_SOURCE_ROOT=str(root), AGENTSTRATA_APPLICATION_VERSION=application_identity(root)["source_digest"], **{STOP_EVENT: event_name(root, service.name)})
                prefixes = [str(root / "src"), *(str(root / Path(value).parent)
                            for value in load_components(root)[2].values())]
                environment["PYTHONPATH"] = os.pathsep.join(prefixes)
                command = [service.command[0], "-m", "chatcopilot.core.release_runtime", "--control",
                           str(installed.control), "--service", service.name, "--component", "chatcopilot"]
                if service.name == "gateway":
                    command += ["--bot", str(root / installed.bot_relative)]
                else:
                    for name in load_components(root)[2]:
                        command += ["--component", name]
                command += ["--", *service.command]
                log_path = private_directory(root / ".agentstrata-data/logs") / (service.name + ".log")
                output = log_path.open("ab")
                logs.append(output)
                child = job.spawn(command, cwd=service.cwd, env=environment, output=output)
                children.append((child, event))
                deadline = time.monotonic() + startup_timeout
                while not UserSystemd.ready(service):
                    if child.poll() is not None:
                        raise RuntimeError("Native service exited during startup: " + service.name)
                    if stopping.is_set() or win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
                        return 0
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Native service readiness timed out: " + service.name)
                    time.sleep(0.1)
            ready = observe(root)
            if not ready["ok"]:
                raise RuntimeError("Native services have no matching loaded-source receipts")
            print(json.dumps({"ok": True, "state": "ready", "bot_id": installed.bot_id}), flush=True)
            while not stopping.wait(0.1):
                if win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
                    break
                if any(child.poll() is not None for child, _ in children):
                    raise RuntimeError("An owned native service exited unexpectedly")
            return 0
        finally:
            stopping.set()
            deadline = time.monotonic() + shutdown_timeout
            for child, event in reversed(children):
                win32event.SetEvent(event)
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                child.close()
            for output in logs:
                output.close()
            (installed.control / "supervisor.json").unlink(missing_ok=True)
            for handle in handles:
                handle.Close()
            for number, handler in previous.items():
                signal.signal(number, handler)
            job.close()
