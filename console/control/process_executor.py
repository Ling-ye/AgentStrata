"""Subprocess boundary shared by console operations."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence


def run_capture(
    args: Sequence[str], *, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    """Run argv with the systemd user-manager environment and captured text."""
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        shell=False,
    )


__all__ = ["run_capture"]
