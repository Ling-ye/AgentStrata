"""Single subprocess execution boundary for Codex CLI invocations."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def run_codex_process(
    command: list[str],
    *,
    cwd: Path,
    prompt: str,
    timeout_seconds: int,
    env: dict[str, str],
    runner: ProcessRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute argv without a shell using the repository-wide text contract."""
    execute = runner or subprocess.run
    return execute(
        command,
        cwd=str(cwd),
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1, int(timeout_seconds)),
        shell=False,
        env=env,
    )


__all__ = ["run_codex_process"]
