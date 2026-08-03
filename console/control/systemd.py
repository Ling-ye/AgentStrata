"""systemctl --user 薄封装。

后端通过这里启停/查询每实例的 chatcopilot@<id> 服务；所有命令显式补好
XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS，避免在非登录会话（如控制台后端自身
也跑成服务时）拿不到 user manager。
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List


def _user_env() -> Dict[str, str]:
    env = dict(os.environ)
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return env
    uid = getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault(
        "DBUS_SESSION_BUS_ADDRESS",
        f"unix:path=/run/user/{uid}/bus",
    )
    return env


def _run(args: List[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=_user_env(),
        timeout=timeout,
    )


def _systemctl(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args], timeout=timeout)


def is_available() -> bool:
    """user manager 是否可用（WSL 未开 systemd 时返回 False）。"""
    try:
        cp = _run(["systemctl", "--user", "is-system-running"], timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return False
    # running / degraded 都算可用
    return cp.returncode == 0 or (cp.stdout or "").strip() in {"degraded", "starting", "running"}


def unit_installed() -> bool:
    cp = _systemctl("cat", "chatcopilot@.service", timeout=5.0)
    return cp.returncode == 0


def show(unit: str) -> Dict[str, str]:
    props = [
        "ActiveState",
        "SubState",
        "UnitFileState",
        "MainPID",
        "ActiveEnterTimestamp",
        "ExecMainStatus",
        "Result",
        "LoadState",
    ]
    cp = _systemctl("show", unit, *(f"-p{p}" for p in props), timeout=10.0)
    out: Dict[str, str] = {}
    for line in (cp.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def action(verb: str, unit: str) -> subprocess.CompletedProcess:
    """verb ∈ start|stop|restart|enable|disable。"""
    if verb not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError(f"不支持的 systemctl 动作：{verb}")
    return _systemctl(verb, unit, timeout=60.0)
