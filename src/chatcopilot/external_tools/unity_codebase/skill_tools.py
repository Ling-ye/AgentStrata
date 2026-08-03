"""``unity.skills`` tool implementations.

Thin wrappers around skill scripts that ship inside each registered Unity
project. Per the S-A design decision, every skill gets its own dedicated tool
(rather than a generic dispatcher) so the LLM sees stable function-call
schemas and can pick the right tool without guessing.

Currently exposes ``unity_path_book`` (a wrapper over the project-side
``.claude/skills/path-book/scripts/path_book.py``). Add more wrappers here as
project skills mature.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from chatcopilot.external_tools.shared.spec_helpers import (
    require_arg,
    schema_property,
    validate_choice,
)
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef
from chatcopilot.external_tools.unity_codebase.config import (
    UnityProjectConfig,
    load_registry,
)

_CATEGORY = "unity.skills"
_OWNER = "unity_codebase"
_DEFAULT_PROJECT = "sample_game"
_DEFAULT_TIMEOUT_SECS = 30

_PATH_BOOK_MODES = ("keyword", "lua_script", "c_sharp_script")


def _tool(**kwargs: Any) -> ToolDef:
    return ToolDef(category=_CATEGORY, owner=_OWNER, module=__name__, **kwargs)


def _resolve_project(args: Dict[str, Any]) -> UnityProjectConfig:
    registry = load_registry()
    project_id = (args.get("project") or "").strip() or registry.default_id
    return registry.get(project_id)


def _resolve_skill_script(project: UnityProjectConfig, skill_name: str) -> Path:
    rel = project.skills.get(skill_name)
    if not rel:
        raise FileNotFoundError(
            f"project {project.project_id!r} does not declare skill {skill_name!r} "
            f"in projects.yaml (skills mapping)"
        )
    script = project.root / rel
    if not script.is_file():
        raise FileNotFoundError(f"skill script not found: {script}")
    return script


def _python_executable() -> str:
    """Pick a Python interpreter to invoke skill scripts with.

    Prefer the current ``sys.executable`` (the same interpreter AgentStrata runs
    under). Fall back to ``python3`` on PATH for unusual deployments.
    """
    if sys.executable and Path(sys.executable).is_file():
        return sys.executable
    found = shutil.which("python3") or shutil.which("python")
    if not found:
        raise RuntimeError("no Python interpreter found on PATH to run project skills")
    return found


# ---------------------------------------------------------------------------
# unity_path_book
# ---------------------------------------------------------------------------
def _handler_path_book(args: Dict[str, Any]) -> HandlerResult:
    project = _resolve_project(args)
    mode = require_arg(args, "mode")
    validate_choice(mode, _PATH_BOOK_MODES, name="mode")
    pattern = require_arg(args, "pattern")

    script = _resolve_skill_script(project, "path_book")
    cmd: List[str] = [
        _python_executable(),
        str(script),
        "-m",
        mode,
        "-p",
        pattern,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(project.root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_DEFAULT_TIMEOUT_SECS,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"path_book exited with rc={proc.returncode}; stderr: {proc.stderr.strip() or '<empty>'}"
        )

    stdout = (proc.stdout or "").rstrip("\n")
    if not stdout.strip():
        summary = (
            f"unity_path_book mode={mode!r} pattern={pattern!r} in project {project.project_id!r}: "
            f"no matches"
        )
    else:
        summary = (
            f"unity_path_book mode={mode!r} pattern={pattern!r} in project {project.project_id!r}:\n"
            f"{stdout}"
        )
    return summary, [], None


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------
def _project_property() -> Dict[str, Any]:
    return schema_property(
        type="string",
        description="Logical project id from unity_codebase/projects.yaml. Defaults to 'sample_game'.",
        default=_DEFAULT_PROJECT,
    )


_PROPS_PATH_BOOK: Dict[str, Dict[str, Any]] = {
    "mode": schema_property(
        type="string",
        description=(
            "path_book lookup mode: 'keyword' searches ForAI docs by their KEYWORD_EN/CN metadata; "
            "'lua_script' looks up a Lua script path by name; 'c_sharp_script' looks up a C# script path."
        ),
        enum=list(_PATH_BOOK_MODES),
    ),
    "pattern": schema_property(
        type="string",
        description=(
            "Comma-separated keywords (for mode='keyword') or a script name fragment "
            "(for the other modes). Matching rules follow the project-side path_book README."
        ),
    ),
    "project": _project_property(),
}


TOOLS: List[ToolDef] = [
    _tool(
        name="unity_path_book",
        summary=(
            "First-jump path routing for a registered Unity project: invokes the project's bundled "
            ".claude/skills/path-book script and returns candidate file/document paths for the "
            "given keywords or script name. Use this whenever you do not yet know where the relevant "
            "code or doc lives, then drill in with unity.codebase.read tools."
        ),
        properties=_PROPS_PATH_BOOK,
        required=["mode", "pattern"],
        handler=_handler_path_book,
        aliases=["path_book", "unity_route", "unity-path-book"],
    ),
]


__all__ = ["TOOLS"]
