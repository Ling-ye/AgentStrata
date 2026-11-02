"""Installed application lifecycle and live-state queries through deployment adapters."""
from pathlib import Path
import time
from chatcopilot.core.application_installation import application_identity, installation
from chatcopilot.deployment.component_repository import load_components


def desktop_info(root: Path) -> dict:
    """Local native-shell credential handoff; never exposed through a web route."""
    from chatcopilot.deployment.configuration import runtime_environment
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit application installation is required")
    plugins = load_components(root)[3]
    avatar = next((item for item in plugins if item["id"] == "open-llm-vtuber"), None)
    if avatar is None:
        raise ValueError("This application has no Live2D component")
    env = runtime_environment(installed.control / "runtime.env")
    return {"application_id": installed.bot_id, "avatar_url": avatar["url"],
            "control_auth": env["AGENTSTRATA_AVATAR_CONTROL_AUTH"]}


def status(root: Path, *, runtime=None) -> dict:
    identity = application_identity(root)
    if identity is None:
        raise ValueError("No independent application is installed here")
    from chatcopilot.deployment.activation_runtime import source_target
    installed = installation(root)
    plugins = load_components(root)[3]
    try:
        runtime = runtime or source_target(root, installed.bot_relative, installed.bot_id)
        running = runtime.observe()
        return {**identity, "ok": True, "state": "ready", "plugins": plugins,
                "services": {name: {"loaded": row["loaded"], "version": row.get("version")}
                             for name, row in running.items()}}
    except (OSError, ValueError, RuntimeError) as error:
        return {**identity, "ok": False, "state": "not_ready", "plugins": plugins,
                "error_type": type(error).__name__}


def control(root: Path, operation: str, *, runtime=None, timeout: float = 60) -> dict:
    installed = installation(root)
    if installed is None or operation not in {"start", "stop"}:
        raise ValueError("An explicit installed application and start/stop operation are required")
    from chatcopilot.deployment.activation_runtime import source_target
    runtime = runtime or source_target(root, installed.bot_relative, installed.bot_id)
    if operation == "stop":
        runtime.stop()
        return {"ok": True, "state": "stopped", "bot_id": installed.bot_id}
    runtime.start()
    deadline = time.monotonic() + timeout
    while True:
        result = status(root, runtime=runtime)
        if result["ok"] or time.monotonic() >= deadline:
            return result
        time.sleep(0.25)
