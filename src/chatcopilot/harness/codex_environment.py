"""Host-owned Git visibility and tool projection for the Codex execution boundary."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.core.scoped_process import sandbox_command
from chatcopilot.core.source_snapshot import git_output
from chatcopilot.harness.models import HarnessError


def git_metadata(root: Path) -> tuple[Path, ...]:
    try:
        paths = git_output(root, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir").splitlines()
        return tuple(dict.fromkeys(Path(path).resolve(strict=True) for path in paths))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HarnessError("coding_environment", "候选工作区 Git 元数据不可用；模型尚未启动，请检查 Harness 执行环境") from exc


def shell_environment(binary: Path, helper: Path | None) -> dict[str, str]:
    paths = ["/sandbox-tools", *((str(helper),) if helper else ()), str(binary.parent),
             f"{sys.prefix}/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    # pytest-rerunfailures opens an INET socket during configuration, before any
    # test runs. Native role tools deliberately deny that socket capability.
    return {"PATH": ":".join(paths), "TMPDIR": "/tmp", "GIT_OPTIONAL_LOCKS": "0",
            "PYTEST_ADDOPTS": "-p no:rerunfailures"}


def wrap_command(command: list[str], *, scope: ExecutionScope, cwd: Path,
                 environment: dict[str, str], rg: Path | None,
                 bindings: tuple[str, ...] = ()) -> list[str]:
    outer = sandbox_command(command, scope=scope, cwd=cwd)
    extra = [part for name, value in environment.items() for part in ("--setenv", name, value)]
    if rg:
        extra += ["--dir", "/sandbox-tools", "--ro-bind", str(rg), "/sandbox-tools/rg"]
    outer[outer.index("--"):outer.index("--")] = [*extra, *bindings]
    return outer


def check_git(binary: Path, *, scope: ExecutionScope, cwd: Path, root: Path,
              metadata: tuple[Path, ...], expected_head: str, runtime_home: Path, config: tuple[str, ...],
              environment: dict[str, str], rg: Path | None, timeout: int | None,
              check_cancel: Callable[[], None]) -> None:
    # The native CLI's sandbox subcommand runs a local command, without a model
    # or credentials. Use the same nested boundary as the subsequent model tools.
    check_cancel()
    query = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root),
             "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir", "HEAD"]
    command = [str(binary), "sandbox", "-P", "agentstrata", "-C", str(cwd)]
    for entry in config:
        command += ["-c", entry]
    command += ["--", "/bin/bash", "-c", shlex.join(query)]
    try:
        result = subprocess.run(wrap_command(command, scope=scope, cwd=cwd,
            environment=environment, rg=rg, bindings=("--dir", "/sandbox-home/codex-preflight", "--setenv", "CODEX_HOME", "/sandbox-home/codex-preflight",
                "--dir", str(runtime_home))),
            cwd=cwd, capture_output=True, text=True, timeout=min(10, timeout) if timeout is not None else 10, env={"PATH": os.defpath})
        lines = result.stdout.strip().splitlines()
        expected_dirs = set(map(str, metadata))
        if (result.returncode or len(lines) != 4 or lines[0] != str(root)
                or set(lines[1:3]) != expected_dirs or lines[3] != expected_head):
            raise HarnessError("coding_environment", "Codex 沙箱无法读取候选工作区的 Git 上下文或基准提交不符；模型尚未启动。"
                               + f"环境检查退出码 {result.returncode}：" + result.stderr.strip()[-1000:])
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessError("coding_environment", "Codex 沙箱 Git 环境检查未能完成；模型尚未启动") from exc
    check_cancel()
