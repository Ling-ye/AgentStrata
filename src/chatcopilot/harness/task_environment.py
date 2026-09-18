"""Host-prepared task dependencies; candidate code never installs into the worker."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.harness.models import HarnessError


def prepare_environment(directory: Path, source: Path, cancel) -> dict[str, str]:
    root = private_directory(directory / "environment")
    identity = hashlib.sha256(b"".join((source / name).read_bytes() for name in ("pyproject.toml", "uv.lock"))).hexdigest()
    python = root / "venv/bin/python"
    receipt = root / "receipt.json"
    if receipt.exists():
        private_file(receipt)
        state = json.loads(receipt.read_text())
        if state.get("declarations") != identity or state.get("base_python") != sys.executable or not python.is_file():
            raise HarnessError("environment_changed", "任务依赖环境与冻结声明不符")
        return state
    configured = os.environ.get("CHATCOPILOT_HARNESS_UV_BIN") or shutil.which("uv")
    if not configured:
        installed = list((Path.home() / ".local/share/agentstrata/uv").glob("*/bin/uv"))
        configured = str(installed[0]) if len(installed) == 1 else ""
    if not configured or not Path(configured).is_absolute() or not Path(configured).is_file():
        raise HarnessError("dependencies_missing", "需要 uv；请安装 WSL 声明工具或配置 CHATCOPILOT_HARNESS_UV_BIN")
    command = [configured, "sync", "--project", str(source), "--frozen", "--no-config",
               "--no-install-project", "--no-editable", "--no-python-downloads", "--python", sys.executable,
               "--link-mode", "copy", "--extra", "agent", "--extra", "acp", "--extra", "dev"]
    environment = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(root / "venv")}
    log = root / "prepare.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            while process.poll() is None:
                cancel()
                time.sleep(.1)
            cancel()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    log.chmod(0o600)
    if process.returncode or not python.is_file():
        raise HarnessError("dependencies_missing", "冻结依赖准备未完成；查看任务 environment/prepare.log")
    state = {"declarations": identity, "base_python": sys.executable, "python": str(python), "root": str(root)}
    receipt.write_text(json_text(state))
    receipt.chmod(0o600)
    return state
