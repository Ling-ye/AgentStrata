"""``filesystem.windows.read`` tool implementations: ``win_read_file`` / ``win_grep`` / ``win_glob``.

These tools take **absolute** Windows / WSL paths and operate against the
global allow-list in :mod:`chatcopilot.external_tools.windows_fs.config`. They
are deliberately project-agnostic; for Unity-project-aware code search use the
``unity_codebase`` package instead.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict, List, Tuple

from chatcopilot.external_tools.shared.spec_helpers import (
    require_arg,
    schema_property,
    validate_non_negative,
)
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef
from chatcopilot.external_tools.windows_fs.config import load_config
from chatcopilot.external_tools.windows_fs.path_guard import (
    PathAccessError,
    ensure_directory_searchable,
    ensure_readable,
)

_CATEGORY = "filesystem.windows.read"
_OWNER = "windows_fs"
_DEFAULT_TIMEOUT_SECS = 30


def _win_tool(**kwargs: Any) -> ToolDef:
    return ToolDef(category=_CATEGORY, owner=_OWNER, module=__name__, **kwargs)


def _ensure_ripgrep() -> str:
    rg = shutil.which("rg")
    if not rg:
        raise RuntimeError(
            "ripgrep (rg) is not installed. Install it inside the WSL/host where "
            "AgentStrata runs (e.g. `sudo apt install ripgrep`)."
        )
    return rg


def _run_subprocess(cmd: List[str], *, timeout: int = _DEFAULT_TIMEOUT_SECS) -> Tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# win_read_file
# ---------------------------------------------------------------------------
def _handler_win_read_file(args: Dict[str, Any]) -> HandlerResult:
    cfg = load_config()
    path = require_arg(args, "path")
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    target = ensure_readable(path, cfg)
    if not target.exists():
        raise FileNotFoundError(f"file not found: {target}")
    if target.is_dir():
        raise IsADirectoryError(f"path is a directory, use win_glob to list it: {target}")

    size = target.stat().st_size
    if size > cfg.max_read_bytes:
        raise ValueError(
            f"file too large ({size} bytes > max_read_bytes={cfg.max_read_bytes}): {target}. "
            f"Use win_grep to search inside it, or read a line range."
        )

    with target.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    total = len(lines)
    s = int(start_line) if start_line not in (None, "", 0) else 1
    e = int(end_line) if end_line not in (None, "") else total
    if s < 1:
        s = 1
    if e > total:
        e = total
    if e < s:
        raise ValueError(f"end_line ({e}) is before start_line ({s})")

    selected = lines[s - 1 : e]
    body = "".join(selected).rstrip("\n")
    header = f"# {target} (lines {s}-{e} of {total})"
    summary = f"{header}\n{body}" if body else f"{header}\n<empty>"
    return summary, [str(target)], None


# ---------------------------------------------------------------------------
# win_grep
# ---------------------------------------------------------------------------
def _handler_win_grep(args: Dict[str, Any]) -> HandlerResult:
    cfg = load_config()
    query = require_arg(args, "query")
    raw_path = require_arg(args, "path")
    file_glob = (args.get("file_glob") or "").strip()
    max_count = int(args.get("max_count", 200))
    validate_non_negative(max_count, name="max_count")

    search_root = ensure_directory_searchable(raw_path, cfg)
    if not search_root.exists():
        raise FileNotFoundError(f"search root not found: {search_root}")

    rg = _ensure_ripgrep()
    cmd: List[str] = [
        rg,
        "--no-heading",
        "--with-filename",
        "-n",
        "--color=never",
        "-m",
        str(max_count),
    ]
    if file_glob:
        cmd += ["-g", file_glob]
    for deny in cfg.denied_patterns:
        cmd += ["-g", f"!{deny}"]
    if cfg.allowed_extensions:
        ext_brace = ",".join(ext.lstrip(".") for ext in cfg.allowed_extensions if ext)
        if ext_brace and not file_glob:
            cmd += ["-g", f"*.{{{ext_brace}}}"]
    cmd += [query, str(search_root)]

    rc, stdout, stderr = _run_subprocess(cmd)
    if rc not in (0, 1):
        raise RuntimeError(f"ripgrep failed (rc={rc}): {stderr.strip() or stdout.strip()}")

    hits = stdout.rstrip("\n").splitlines()
    if not hits:
        summary = f"win_grep: no matches for {query!r} under {search_root}"
        return summary, [], None

    body = "\n".join(hits[: max_count or len(hits)])
    summary = f"win_grep: {len(hits)} hits for {query!r} under {search_root}\n{body}"
    return summary, [], None


# ---------------------------------------------------------------------------
# win_glob
# ---------------------------------------------------------------------------
def _handler_win_glob(args: Dict[str, Any]) -> HandlerResult:
    cfg = load_config()
    pattern = require_arg(args, "pattern")
    raw_path = require_arg(args, "path")
    limit = int(args.get("limit", 200))
    validate_non_negative(limit, name="limit")

    search_root = ensure_directory_searchable(raw_path, cfg)
    if not search_root.exists():
        raise FileNotFoundError(f"search root not found: {search_root}")

    rg = _ensure_ripgrep()
    cmd: List[str] = [rg, "--files", "--color=never"]
    for deny in cfg.denied_patterns:
        cmd += ["-g", f"!{deny}"]
    cmd += ["-g", pattern, str(search_root)]

    rc, stdout, stderr = _run_subprocess(cmd)
    if rc not in (0, 1):
        raise RuntimeError(f"ripgrep --files failed (rc={rc}): {stderr.strip() or stdout.strip()}")

    files = [line for line in stdout.splitlines() if line.strip()]
    if not files:
        summary = f"win_glob: no files match {pattern!r} under {search_root}"
        return summary, [], None

    truncated = files[:limit] if limit else files
    body = "\n".join(truncated)
    note = "" if len(files) <= len(truncated) else f"\n... ({len(files) - len(truncated)} more truncated)"
    summary = f"win_glob: {len(files)} files match {pattern!r} under {search_root}\n{body}{note}"
    return summary, [], None


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------
_PROPS_READ: Dict[str, Dict[str, Any]] = {
    "path": schema_property(
        type="string",
        description="Absolute path of the file to read. Must lie under configured windows_fs allowed_roots.",
    ),
    "start_line": schema_property(
        type="integer",
        description="Optional 1-based start line, inclusive. Omit to start at line 1.",
    ),
    "end_line": schema_property(
        type="integer",
        description="Optional 1-based end line, inclusive. Omit to read until EOF.",
    ),
}

_PROPS_GREP: Dict[str, Dict[str, Any]] = {
    "query": schema_property(
        type="string",
        description="Regular expression / fixed string to search for (ripgrep syntax).",
    ),
    "path": schema_property(
        type="string",
        description="Absolute directory under which to search. Must lie under windows_fs allowed_roots.",
    ),
    "file_glob": schema_property(
        type="string",
        description="Optional ripgrep -g pattern to limit by filename (e.g. '*.cs').",
        default="",
    ),
    "max_count": schema_property(
        type="integer",
        description="Cap total hits returned. Default 200.",
        default=200,
    ),
}

_PROPS_GLOB: Dict[str, Dict[str, Any]] = {
    "pattern": schema_property(
        type="string",
        description="Ripgrep -g pattern (e.g. '**/Mission*.cs') for file name matching.",
    ),
    "path": schema_property(
        type="string",
        description="Absolute directory under which to list matching files.",
    ),
    "limit": schema_property(
        type="integer",
        description="Maximum number of files to return. Default 200.",
        default=200,
    ),
}


TOOLS: List[ToolDef] = [
    _win_tool(
        name="win_read_file",
        summary=(
            "Read a text file by absolute path from the Windows file system (works in WSL via /mnt/f/...). "
            "Supports an optional line range. Subject to the windows_fs allow-list and a max_read_bytes cap. "
            "Prefer unity_project_read when the file lives inside a registered Unity project."
        ),
        properties=_PROPS_READ,
        required=["path"],
        handler=_handler_win_read_file,
        aliases=["read_windows_file", "win-read"],
    ),
    _win_tool(
        name="win_grep",
        summary=(
            "ripgrep over a Windows / WSL directory by absolute path. Filters by file_glob and the global allow-list. "
            "Prefer unity_project_search inside Unity projects; this tool is for one-off probes outside any registered project."
        ),
        properties=_PROPS_GREP,
        required=["query", "path"],
        handler=_handler_win_grep,
        aliases=["grep_windows", "win-grep"],
    ),
    _win_tool(
        name="win_glob",
        summary=(
            "List files matching a ripgrep -g pattern under an absolute Windows / WSL directory, "
            "honoring the windows_fs allow-list and deny patterns."
        ),
        properties=_PROPS_GLOB,
        required=["pattern", "path"],
        handler=_handler_win_glob,
        aliases=["glob_windows", "win-glob"],
    ),
]


__all__ = ["TOOLS", "PathAccessError"]
