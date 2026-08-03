"""Internal ripgrep helper used by ``unity_codebase`` tools.

Centralizes subprocess construction, output decoding and timeout handling so
``read_tools.py`` stays focused on declaring tool semantics.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from chatcopilot.external_tools.unity_codebase.config import UnityProjectConfig

_DEFAULT_TIMEOUT_SECS = 30


def ensure_ripgrep() -> str:
    rg = shutil.which("rg")
    if not rg:
        raise RuntimeError(
            "ripgrep (rg) is not installed in the runtime environment. "
            "Install it where AgentStrata runs (e.g. `sudo apt install ripgrep` in WSL)."
        )
    return rg


def project_deny_args(project: UnityProjectConfig) -> List[str]:
    """Convert the project deny_globs into ripgrep ``-g !pattern`` flags."""
    args: List[str] = []
    for pattern in project.deny_globs:
        if pattern.strip():
            args += ["-g", f"!{pattern}"]
    return args


def project_default_ext_glob(project: UnityProjectConfig) -> List[str]:
    """If the project declares allow_extensions, restrict ripgrep to those files."""
    if not project.allow_extensions:
        return []
    exts = ",".join(ext.lstrip(".") for ext in project.allow_extensions if ext)
    if not exts:
        return []
    return ["-g", f"*.{{{exts}}}"]


def run_ripgrep(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECS,
) -> Tuple[int, str, str]:
    proc = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def build_search_command(
    project: UnityProjectConfig,
    *,
    pattern: str,
    search_root: Path,
    file_glob: str | None = None,
    max_count: int = 200,
    extra_args: Iterable[str] = (),
    use_default_ext_glob: bool = True,
) -> List[str]:
    """Assemble the ripgrep argv for a project-scoped content search."""
    rg = ensure_ripgrep()
    cmd: List[str] = [
        rg,
        "--no-heading",
        "--with-filename",
        "-n",
        "--color=never",
        "-m",
        str(max_count),
    ]
    cmd += list(extra_args)
    if file_glob and file_glob.strip():
        cmd += ["-g", file_glob]
    elif use_default_ext_glob:
        cmd += project_default_ext_glob(project)
    cmd += project_deny_args(project)
    cmd += [pattern, str(search_root)]
    return cmd


def build_files_command(
    project: UnityProjectConfig,
    *,
    search_root: Path,
    file_glob: str,
) -> List[str]:
    """Assemble ``rg --files -g pattern`` for project-scoped filename listing."""
    rg = ensure_ripgrep()
    cmd: List[str] = [rg, "--files", "--color=never"]
    cmd += project_deny_args(project)
    cmd += ["-g", file_glob, str(search_root)]
    return cmd


__all__ = [
    "build_files_command",
    "build_search_command",
    "ensure_ripgrep",
    "project_default_ext_glob",
    "project_deny_args",
    "run_ripgrep",
]
