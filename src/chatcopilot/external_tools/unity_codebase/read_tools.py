"""``unity.codebase.read`` tool implementations.

Four project-aware tools for browsing a registered Unity project:

* ``unity_project_read``       - read a file by ``project`` + ``rel_path``.
* ``unity_project_search``     - ripgrep content search inside the project.
* ``unity_project_glob``       - list files matching a ripgrep -g pattern.
* ``unity_find_csharp_symbol`` - C# semantic search across four modes
  (``definition`` / ``references`` / ``new_expression`` / ``callers``).
"""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.external_tools.shared.spec_helpers import (
    require_arg,
    schema_property,
    validate_non_negative,
)
from chatcopilot.external_tools.shared.tool_spec import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)
from chatcopilot.external_tools.unity_codebase._csharp_patterns import (
    build_csharp_query,
    supported_modes,
)
from chatcopilot.external_tools.unity_codebase._ripgrep import (
    build_files_command,
    build_search_command,
    run_ripgrep,
)
from chatcopilot.external_tools.unity_codebase.config import (
    UnityProjectConfig,
    load_registry,
)
from chatcopilot.external_tools.unity_codebase.path_guard import (
    ensure_readable,
    ensure_searchable,
)

_CATEGORY = "unity.codebase.read"
_OWNER = "unity_codebase"
_DEFAULT_PROJECT = "sample_game"


def _tool(**kwargs: Any) -> ToolDef:
    return ToolDef(category=_CATEGORY, owner=_OWNER, module=__name__, **kwargs)


def _resolve_project(args: Dict[str, Any]) -> UnityProjectConfig:
    registry = load_registry()
    project_id = (args.get("project") or "").strip() or registry.default_id
    return registry.get(project_id)


def _format_hit_summary(
    *,
    label: str,
    project: UnityProjectConfig,
    hits: List[str],
    max_count: int,
) -> str:
    if not hits:
        return f"{label}: no matches in project {project.project_id!r}"
    body = "\n".join(hits[:max_count]) if max_count else "\n".join(hits)
    more = "" if len(hits) <= (max_count or len(hits)) else f"\n... ({len(hits) - max_count} more truncated)"
    return f"{label}: {len(hits)} hits in project {project.project_id!r}\n{body}{more}"


# ---------------------------------------------------------------------------
# unity_project_read
# ---------------------------------------------------------------------------
def _handler_project_read(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    project = _resolve_project(args)
    rel_path = require_arg(args, "rel_path")
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    abs_path, norm_rel = ensure_readable(project, rel_path)
    if not abs_path.exists():
        raise FileNotFoundError(
            f"file not found in project {project.project_id!r}: {norm_rel} -> {abs_path}"
        )
    if abs_path.is_dir():
        raise IsADirectoryError(
            f"rel_path is a directory; use unity_project_glob to list it: {norm_rel}"
        )

    size = abs_path.stat().st_size
    if size > project.max_read_bytes:
        raise ValueError(
            f"file too large ({size} bytes > max_read_bytes={project.max_read_bytes}): {norm_rel}. "
            f"Use unity_project_search to search inside it, or read a line range."
        )

    with abs_path.open("r", encoding="utf-8", errors="replace") as fh:
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

    body = "".join(lines[s - 1 : e]).rstrip("\n")
    header = f"# project={project.project_id} {norm_rel} (lines {s}-{e} of {total})"
    summary = f"{header}\n{body}" if body else f"{header}\n<empty>"
    return ToolResult(
        ok=True,
        summary=summary,
        outputs=[str(abs_path)],
        data={
            "project": project.project_id,
            "path": norm_rel,
            "content": body,
            "start_line": s,
            "end_line": e,
            "total_lines": total,
        },
    )


# ---------------------------------------------------------------------------
# unity_project_search
# ---------------------------------------------------------------------------
def _handler_project_search(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    project = _resolve_project(args)
    query = require_arg(args, "query")
    rel_subdir = (args.get("rel_subdir") or "").strip()
    file_glob = (args.get("file_glob") or "").strip()
    max_count = int(args.get("max_count", 200))
    validate_non_negative(max_count, name="max_count")

    search_root, _ = ensure_searchable(project, rel_subdir)
    if not search_root.exists():
        raise FileNotFoundError(
            f"search root not found in project {project.project_id!r}: {search_root}"
        )

    cmd = build_search_command(
        project,
        pattern=query,
        search_root=search_root,
        file_glob=file_glob or None,
        max_count=max_count,
    )
    rc, stdout, stderr = run_ripgrep(cmd)
    if rc not in (0, 1):
        raise RuntimeError(f"ripgrep failed (rc={rc}): {stderr.strip() or stdout.strip()}")

    hits = stdout.rstrip("\n").splitlines() if stdout.strip() else []
    summary = _format_hit_summary(
        label=f"unity_project_search query={query!r}",
        project=project,
        hits=hits,
        max_count=max_count,
    )
    return ToolResult(
        ok=True,
        summary=summary,
        data={"project": project.project_id, "query": query, "hits": hits[:max_count]},
    )


# ---------------------------------------------------------------------------
# unity_project_glob
# ---------------------------------------------------------------------------
def _handler_project_glob(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    project = _resolve_project(args)
    pattern = require_arg(args, "pattern")
    rel_subdir = (args.get("rel_subdir") or "").strip()
    limit = int(args.get("limit", 200))
    validate_non_negative(limit, name="limit")

    search_root, _ = ensure_searchable(project, rel_subdir)
    if not search_root.exists():
        raise FileNotFoundError(
            f"search root not found in project {project.project_id!r}: {search_root}"
        )

    cmd = build_files_command(project, search_root=search_root, file_glob=pattern)
    rc, stdout, stderr = run_ripgrep(cmd)
    if rc not in (0, 1):
        raise RuntimeError(f"ripgrep --files failed (rc={rc}): {stderr.strip() or stdout.strip()}")

    files = [line for line in stdout.splitlines() if line.strip()]
    summary = _format_hit_summary(
        label=f"unity_project_glob pattern={pattern!r}",
        project=project,
        hits=files,
        max_count=limit,
    )
    return ToolResult(
        ok=True,
        summary=summary,
        data={"project": project.project_id, "pattern": pattern, "files": files[:limit]},
    )


# ---------------------------------------------------------------------------
# unity_find_csharp_symbol
# ---------------------------------------------------------------------------
def _handler_find_csharp_symbol(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    project = _resolve_project(args)
    symbol = require_arg(args, "symbol")
    mode = require_arg(args, "mode")
    max_count = int(args.get("max_count", 200))
    validate_non_negative(max_count, name="max_count")

    pattern, default_glob = build_csharp_query(symbol, mode)
    search_root = project.root
    if not search_root.exists():
        raise FileNotFoundError(f"project root missing: {search_root}")

    cmd = build_search_command(
        project,
        pattern=pattern,
        search_root=search_root,
        file_glob=default_glob,
        max_count=max_count,
        use_default_ext_glob=False,
    )
    rc, stdout, stderr = run_ripgrep(cmd)
    if rc not in (0, 1):
        raise RuntimeError(f"ripgrep failed (rc={rc}): {stderr.strip() or stdout.strip()}")

    hits = stdout.rstrip("\n").splitlines() if stdout.strip() else []
    label = f"unity_find_csharp_symbol symbol={symbol!r} mode={mode!r}"
    summary = _format_hit_summary(label=label, project=project, hits=hits, max_count=max_count)
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "project": project.project_id,
            "symbol": symbol,
            "mode": mode,
            "hits": hits[:max_count],
        },
    )


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------
def _project_property(default: str = _DEFAULT_PROJECT) -> Dict[str, Any]:
    return schema_property(
        type="string",
        description="Logical project id from unity_codebase/projects.yaml. Defaults to 'sample_game'.",
        default=default,
    )


_PROPS_READ: Dict[str, Dict[str, Any]] = {
    "rel_path": schema_property(
        type="string",
        description="File path relative to the project root (e.g. 'Assets/Scripts/Mission/MissionPanel.cs').",
    ),
    "project": _project_property(),
    "start_line": schema_property(
        type="integer",
        description="Optional 1-based start line, inclusive.",
    ),
    "end_line": schema_property(
        type="integer",
        description="Optional 1-based end line, inclusive.",
    ),
}

_PROPS_SEARCH: Dict[str, Dict[str, Any]] = {
    "query": schema_property(
        type="string",
        description="Regex / fixed-string passed to ripgrep.",
    ),
    "project": _project_property(),
    "rel_subdir": schema_property(
        type="string",
        description="Optional subdirectory under the project root to constrain the search.",
        default="",
    ),
    "file_glob": schema_property(
        type="string",
        description="Optional ripgrep -g filename filter (e.g. '*.cs' or '*Mission*.cs').",
        default="",
    ),
    "max_count": schema_property(
        type="integer",
        description="Cap on total hits returned. Default 200.",
        default=200,
    ),
}

_PROPS_GLOB: Dict[str, Dict[str, Any]] = {
    "pattern": schema_property(
        type="string",
        description="ripgrep -g pattern (e.g. '**/Mission*.cs').",
    ),
    "project": _project_property(),
    "rel_subdir": schema_property(
        type="string",
        description="Optional subdirectory under the project root to constrain listing.",
        default="",
    ),
    "limit": schema_property(
        type="integer",
        description="Maximum number of files to return. Default 200.",
        default=200,
    ),
}

_PROPS_CSHARP: Dict[str, Dict[str, Any]] = {
    "symbol": schema_property(
        type="string",
        description="C# identifier to query (class / struct / method / field name).",
    ),
    "mode": schema_property(
        type="string",
        description=(
            "Query mode: 'definition' (find type definition), 'references' (all occurrences), "
            "'new_expression' (where `new Symbol(...)` is invoked - use this for memory creation "
            "tracing), 'callers' (locations that look like `Symbol(...)` calls)."
        ),
        enum=list(supported_modes()),
    ),
    "project": _project_property(),
    "max_count": schema_property(
        type="integer",
        description="Cap on total hits returned. Default 200.",
        default=200,
    ),
}

_HITS_RESULT_SCHEMA = object_schema(
    {
        "project": {"type": "string"},
        "query": {"type": "string"},
        "hits": {"type": "array", "items": {"type": "string"}},
    },
    required=("project", "query", "hits"),
)


TOOLS: List[ToolDef] = [
    _tool(
        name="unity_project_read",
        summary=(
            "Read a file from a registered Unity project by project id + relative path. "
            "Honors per-project allow_globs / deny_globs / allow_extensions and a max_read_bytes cap. "
            "Use this for C#/Lua/yaml/json/md files inside the project."
        ),
        input_schema=object_schema(_PROPS_READ, required=("rel_path",)),
        output_schema=object_schema(
            {
                "project": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "total_lines": {"type": "integer"},
            },
            required=("project", "path", "content", "start_line", "end_line", "total_lines"),
        ),
        handler=_handler_project_read,
        aliases=["unity_read", "unity-read"],
    ),
    _tool(
        name="unity_project_search",
        summary=(
            "ripgrep over a registered Unity project (or a subdirectory). "
            "For C# symbol-level queries (definition / new / callers) prefer unity_find_csharp_symbol."
        ),
        input_schema=object_schema(_PROPS_SEARCH, required=("query",)),
        output_schema=_HITS_RESULT_SCHEMA,
        handler=_handler_project_search,
        aliases=["unity_search", "unity_grep"],
    ),
    _tool(
        name="unity_project_glob",
        summary=(
            "List files in a registered Unity project matching a ripgrep -g pattern. "
            "Respects project deny_globs and uses ripgrep's gitignore-aware traversal."
        ),
        input_schema=object_schema(_PROPS_GLOB, required=("pattern",)),
        output_schema=object_schema(
            {
                "project": {"type": "string"},
                "pattern": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
            },
            required=("project", "pattern", "files"),
        ),
        handler=_handler_project_glob,
        aliases=["unity_glob", "unity_list_files"],
    ),
    _tool(
        name="unity_find_csharp_symbol",
        summary=(
            "C# semantic search across *.cs files in a registered Unity project, using one of four "
            "modes. The 'new_expression' mode is the recommended first step when a user asks where a "
            "memory object / list / instance is created; follow up with unity_project_read for "
            "context, then re-query with mode='callers' on the enclosing method to walk the call chain."
        ),
        input_schema=object_schema(_PROPS_CSHARP, required=("symbol", "mode")),
        output_schema=object_schema(
            {
                "project": {"type": "string"},
                "symbol": {"type": "string"},
                "mode": {"type": "string"},
                "hits": {"type": "array", "items": {"type": "string"}},
            },
            required=("project", "symbol", "mode", "hits"),
        ),
        handler=_handler_find_csharp_symbol,
        aliases=["unity_csharp", "find_csharp", "csharp_symbol"],
    ),
]


__all__ = ["TOOLS"]
