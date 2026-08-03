"""Read-only tools for registered source-code repositories."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from chatcopilot.project import ENV_PREFIX
from chatcopilot.external_tools.codebase.config import CodeRepositoryConfig, load_registry
from chatcopilot.external_tools.codebase.fallback_search import (
    list_visible_files,
    search_visible_files,
)
from chatcopilot.external_tools.codebase.path_guard import (
    ensure_readable,
    ensure_searchable,
    matches_any,
)
from chatcopilot.external_tools.codebase.index import (
    find_references,
    search_imports,
    search_symbols,
)
from chatcopilot.external_tools.shared.spec_helpers import (
    require_arg,
    schema_property,
    validate_non_negative,
)
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef

_CATEGORY = "codebase.read"
_OWNER = "codebase"
_MAX_READ_LINES = 500
_RG_TIMEOUT_SECONDS = 30


def _tool(**kwargs: Any) -> ToolDef:
    return ToolDef(
        category=_CATEGORY,
        owner=_OWNER,
        module=__name__,
        requires_role="owner",
        **kwargs,
    )


def _repository(args: dict[str, Any]) -> CodeRepositoryConfig:
    change_id = str(args.get("change_id") or "").strip()
    if change_id:
        from chatcopilot.external_tools.repository_tasks.runtime import change_repository

        return change_repository(change_id)
    return load_registry().get(str(args.get("repository") or "").strip() or None)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handler_list(_: dict[str, Any]) -> HandlerResult:
    registry = load_registry()
    lines = ["# Registered codebase repositories"]
    for repository in registry.repositories.values():
        description = f" - {repository.description}" if repository.description else ""
        state = "available" if repository.root.is_dir() else "missing"
        lines.append(
            f"- {repository.repository_id}: {repository.display_name} [{state}] "
            f"root={repository.root}{description}"
        )
    return "\n".join(lines), [], None


def _handler_map(args: dict[str, Any]) -> HandlerResult:
    repository = _repository(args)
    rel_subdir = str(args.get("rel_subdir") or "").strip()
    depth = int(args["depth"]) if args.get("depth") is not None else 4
    limit = int(args["limit"]) if args.get("limit") is not None else 300
    validate_non_negative(depth, name="depth")
    validate_non_negative(limit, name="limit")
    search_root, normalized = ensure_searchable(repository, rel_subdir)
    _ensure_repository_exists(repository)
    files = _list_files(repository, search_root)
    visible: list[str] = []
    prefix_parts = len(Path(normalized).parts) if normalized else 0
    for rel in files:
        relative_depth = max(0, len(Path(rel).parts) - prefix_parts - 1)
        if relative_depth <= depth:
            visible.append(rel)
        if len(visible) >= limit:
            break
    header = (
        f"# repository={repository.repository_id} structure"
        f" subdir={normalized or '.'} depth={depth}"
    )
    lines = [header, *(f"- {item}" for item in visible)]
    if len(files) > len(visible):
        lines.append(f"... showing {len(visible)} of {len(files)} matched files")
    return "\n".join(lines), [], None


def _handler_search(args: dict[str, Any]) -> HandlerResult:
    repository = _repository(args)
    query = require_arg(args, "query")
    rel_subdir = str(args.get("rel_subdir") or "").strip()
    file_glob = str(args.get("file_glob") or "").strip()
    max_count = int(args["max_count"]) if args.get("max_count") is not None else 100
    validate_non_negative(max_count, name="max_count")
    max_count = min(max_count, 500)
    search_root, normalized = ensure_searchable(repository, rel_subdir)
    _ensure_repository_exists(repository)

    if shutil.which("rg"):
        cmd = _base_rg_command(repository)
        cmd += ["--no-heading", "--with-filename", "--line-number", "--color=never"]
        if bool(args.get("fixed_strings", False)):
            cmd.append("--fixed-strings")
        cmd += ["-e", query, "--", normalized or "."]
        rc, stdout, stderr = _run_rg(cmd, cwd=repository.root)
        if rc not in (0, 1):
            raise RuntimeError(f"ripgrep failed (rc={rc}): {stderr.strip() or stdout.strip()}")
        hits = [
            line for line in stdout.splitlines()
            if line.strip()
            and _visible_path(repository, line.split(":", 1)[0])
            and (
                not file_glob
                or matches_any(line.split(":", 1)[0].replace("\\", "/"), (file_glob,))
            )
        ][:max_count]
    else:
        hits = search_visible_files(
            repository,
            query=query,
            search_root=search_root,
            fixed_strings=bool(args.get("fixed_strings", False)),
            file_glob=file_glob,
            max_count=max_count,
        )
    lines = [
        f"# repository={repository.repository_id} search query={query!r}",
        *hits,
    ]
    if not hits:
        lines.append("<no matches>")
    return "\n".join(lines), [], None


def _handler_read(args: dict[str, Any]) -> HandlerResult:
    repository = _repository(args)
    rel_path = require_arg(args, "rel_path")
    target, normalized = ensure_readable(repository, rel_path)
    if not target.is_file():
        raise FileNotFoundError(
            f"file not found in repository {repository.repository_id!r}: {normalized}"
        )
    size = target.stat().st_size
    if size > repository.max_read_bytes:
        raise ValueError(
            f"file is too large ({size} > {repository.max_read_bytes} bytes): {normalized}"
        )
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(args.get("start_line") or 1))
    requested_end = int(args.get("end_line") or min(len(lines), start + _MAX_READ_LINES - 1))
    end = min(len(lines), requested_end, start + _MAX_READ_LINES - 1)
    if end < start:
        raise ValueError(f"end_line ({end}) is before start_line ({start})")
    numbered = [f"{line_no:>6} | {lines[line_no - 1]}" for line_no in range(start, end + 1)]
    header = (
        f"# repository={repository.repository_id} {normalized} "
        f"(lines {start}-{end} of {len(lines)})"
    )
    return "\n".join([header, *numbered]), [], None


def _handler_symbols(args: dict[str, Any]) -> HandlerResult:
    repository = _repository(args)
    query = str(args.get("query") or "").strip()
    kind = str(args.get("kind") or "").strip()
    parent = str(args.get("parent") or "").strip()
    limit = int(args["limit"]) if args.get("limit") is not None else 100
    validate_non_negative(limit, name="limit")
    stats, hits = search_symbols(repository, query=query, kind=kind, parent=parent, limit=limit)
    lines = [
        f"# repository={repository.repository_id} symbols query={query!r} kind={kind or '*'}",
        f"index: files={stats.files_total} updated={stats.files_updated} removed={stats.files_removed} symbols={stats.symbols_total}",
    ]
    for hit in hits:
        entry = f"- {hit.name} [{hit.kind}] {hit.path}:{hit.line}"
        if hit.parent:
            entry += f" (in {hit.parent})"
        entry += f" - {hit.signature}"
        if hit.docstring:
            entry += f"\n  doc: {hit.docstring}"
        lines.append(entry)
    if not hits:
        lines.append("<no symbols>")
    return "\n".join(lines), [], None


def _handler_references(args: dict[str, Any]) -> HandlerResult:
    """Find all references to a named symbol across the repository."""
    repository = _repository(args)
    symbol = require_arg(args, "symbol")
    exclude_def = bool(args.get("exclude_definition", True))
    limit = int(args["limit"]) if args.get("limit") is not None else 50
    validate_non_negative(limit, name="limit")

    stats, definitions, refs = find_references(
        repository, symbol_name=symbol, exclude_definition=exclude_def, limit=limit,
    )
    lines = [
        f"# repository={repository.repository_id} references for '{symbol}'",
    ]
    if definitions:
        lines.append("## Definitions")
        for d in definitions:
            entry = f"  {d.kind} {d.name} at {d.path}:{d.line}"
            if d.parent:
                entry += f" (in {d.parent})"
            if d.docstring:
                entry += f" — {d.docstring[:80]}"
            lines.append(entry)
    lines.append(f"## References ({len(refs)} found)")
    for ref in refs:
        lines.append(f"  {ref.path}:{ref.line} | {ref.content}")
    if not refs:
        lines.append("  <no references found>")
    return "\n".join(lines), [], None


def _handler_dependencies(args: dict[str, Any]) -> HandlerResult:
    """Analyze import dependencies of a file or module."""
    repository = _repository(args)
    rel_path = str(args.get("rel_path") or "").strip()
    module_query = str(args.get("module") or "").strip()
    direction = str(args.get("direction") or "both").strip().lower()
    limit = int(args["limit"]) if args.get("limit") is not None else 100
    validate_non_negative(limit, name="limit")

    lines = [f"# repository={repository.repository_id} dependencies"]

    if direction in ("upstream", "both") and rel_path:
        # What does this file import?
        _, upstream = search_imports(repository, path=rel_path, limit=limit)
        lines.append(f"## Upstream (imported by {rel_path}): {len(upstream)} imports")
        for imp in upstream:
            prefix = "from" if imp.is_from else "import"
            names_part = f" ({imp.names})" if imp.names else ""
            lines.append(f"  L{imp.line}: {prefix} {imp.module}{names_part}")

    if direction in ("downstream", "both") and (module_query or rel_path):
        # What imports this module?
        target_module = module_query or _path_to_module(rel_path)
        _, downstream = search_imports(repository, module=target_module, limit=limit)
        lines.append(f"## Downstream (imports {target_module}): {len(downstream)} importers")
        for imp in downstream:
            if imp.path == rel_path:
                continue
            names_part = f" ({imp.names})" if imp.names else ""
            lines.append(f"  {imp.path}:L{imp.line}{names_part}")

    if len(lines) == 1:
        lines.append("<provide rel_path or module to analyze dependencies>")
    return "\n".join(lines), [], None


def _handler_context(args: dict[str, Any]) -> HandlerResult:
    """Assemble relevant context for understanding or modifying a code region."""
    repository = _repository(args)
    rel_path = require_arg(args, "rel_path")
    focus_symbol = str(args.get("focus_symbol") or "").strip()
    max_symbols = int(args.get("max_symbols") or 20)
    _ensure_repository_exists(repository)

    lines = [f"# repository={repository.repository_id} context for {rel_path}"]

    # 1. File's own symbols (structure overview)
    _, file_symbols = search_symbols(repository, limit=200)
    own_symbols = [s for s in file_symbols if s.path == rel_path]
    lines.append(f"## File structure ({len(own_symbols)} symbols)")
    for sym in own_symbols[:max_symbols]:
        indent = "  " if sym.parent else ""
        entry = f"{indent}- {sym.kind} {sym.name}"
        if sym.parent:
            entry += f" (in {sym.parent})"
        entry += f" L{sym.line}-{sym.end_line}" if sym.end_line else f" L{sym.line}"
        if sym.docstring:
            entry += f" — {sym.docstring[:60]}"
        lines.append(entry)

    # 2. Imports (what this file depends on)
    _, imports = search_imports(repository, path=rel_path, limit=50)
    if imports:
        lines.append(f"## Imports ({len(imports)})")
        for imp in imports:
            prefix = "from" if imp.is_from else "import"
            names_part = f" ({imp.names})" if imp.names else ""
            lines.append(f"  {prefix} {imp.module}{names_part}")

    # 3. If focus_symbol specified, find its references
    if focus_symbol:
        _, defs, refs = find_references(
            repository, symbol_name=focus_symbol, exclude_definition=True, limit=20,
        )
        lines.append(f"## References to '{focus_symbol}' ({len(refs)} found)")
        for ref in refs[:15]:
            lines.append(f"  {ref.path}:{ref.line} | {ref.content}")

    # 4. Reverse dependencies (who imports this file's module)
    target_module = _path_to_module(rel_path)
    if target_module:
        _, importers = search_imports(repository, module=target_module, limit=20)
        importers = [i for i in importers if i.path != rel_path]
        if importers:
            lines.append(f"## Imported by ({len(importers)} files)")
            for imp in importers[:15]:
                lines.append(f"  {imp.path}:L{imp.line} ({imp.names or '*'})")

    return "\n".join(lines), [], None


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _path_to_module(rel_path: str) -> str:
    """Convert a repo-relative .py path to a dotted module name."""
    if not rel_path.endswith(".py"):
        return ""
    parts = rel_path.replace("\\", "/").removesuffix(".py").split("/")
    # Strip src/ prefix commonly used in src-layout
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else ""


def _ensure_repository_exists(repository: CodeRepositoryConfig) -> None:
    if not repository.root.is_dir():
        raise FileNotFoundError(
            f"registered repository root is missing: {repository.repository_id} "
            f"(resolved root={repository.root}; set {_repository_root_env(repository)} "
            "or fix codebases.registry)"
        )


def _repository_root_env(repository: CodeRepositoryConfig) -> str:
    normalized = "".join(
        ch.upper() if ch.isalnum() else "_" for ch in repository.repository_id
    ).strip("_")
    return f"{ENV_PREFIX}_CODEBASE_{normalized}_ROOT"


def _list_files(repository: CodeRepositoryConfig, search_root: Path) -> list[str]:
    if not shutil.which("rg"):
        return list_visible_files(repository, search_root)
    cmd = _base_rg_command(repository)
    cmd += ["--files", "--", str(search_root.relative_to(repository.root) or ".")]
    rc, stdout, stderr = _run_rg(cmd, cwd=repository.root)
    if rc not in (0, 1):
        raise RuntimeError(f"ripgrep --files failed (rc={rc}): {stderr.strip() or stdout.strip()}")
    return sorted(
        normalized
        for line in stdout.splitlines()
        if line.strip()
        for normalized in [line.replace("\\", "/").removeprefix("./")]
        if _visible_path(repository, normalized)
    )


def _base_rg_command(repository: CodeRepositoryConfig) -> list[str]:
    executable = shutil.which("rg")
    if not executable:
        raise RuntimeError("ripgrep (rg) is required for codebase inspection")
    cmd = [executable]
    for pattern in repository.deny_globs:
        cmd += ["--glob", f"!{pattern}"]
    for pattern in repository.include_globs:
        cmd += ["--glob", pattern]
    if repository.allow_extensions:
        extensions = ",".join(ext.lstrip(".") for ext in repository.allow_extensions)
        cmd += ["--glob", f"*.{{{extensions}}}"]
    return cmd


def _visible_path(repository: CodeRepositoryConfig, rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").removeprefix("./")
    if matches_any(normalized, repository.deny_globs):
        return False
    if repository.include_globs and not matches_any(normalized, repository.include_globs):
        return False
    return not repository.allow_extensions or Path(normalized).suffix.lower() in repository.allow_extensions


def _run_rg(cmd: list[str], *, cwd: Path) -> tuple[int, str, str]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_RG_TIMEOUT_SECONDS,
    )
    return completed.returncode, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_REPOSITORY_PROPERTY = schema_property(
    type="string",
    description="Logical repository id from the current bot's codebase registry.",
)
_CHANGE_PROPERTY = schema_property(
    type="string",
    description="Optional managed change id; when set, inspect that task worktree.",
    default="",
)

TOOLS = [
    _tool(
        name="codebase_list_repositories",
        summary="List source-code repositories registered for the current bot.",
        properties={},
        required=[],
        handler=_handler_list,
    ),
    _tool(
        name="codebase_map",
        summary="Inspect the file and directory structure of a registered code repository.",
        properties={
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "rel_subdir": schema_property(type="string", description="Optional repository-relative directory.", default=""),
            "depth": schema_property(type="integer", description="Maximum relative directory depth.", default=4),
            "limit": schema_property(type="integer", description="Maximum files returned.", default=300),
        },
        required=[],
        handler=_handler_map,
    ),
    _tool(
        name="codebase_search",
        summary="Search source code in a registered repository with ripgrep and return file:line evidence.",
        properties={
            "query": schema_property(type="string", description="Regex or literal text to search for."),
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "rel_subdir": schema_property(type="string", description="Optional repository-relative directory.", default=""),
            "file_glob": schema_property(type="string", description="Optional filename glob such as '*.py'.", default=""),
            "fixed_strings": schema_property(type="boolean", description="Treat query as literal text.", default=False),
            "max_count": schema_property(type="integer", description="Maximum matching lines returned.", default=100),
        },
        required=["query"],
        handler=_handler_search,
    ),
    _tool(
        name="codebase_symbols",
        summary="Incrementally index and query symbols (classes, functions, methods, imports) with scope hierarchy and docstrings.",
        properties={
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "query": schema_property(type="string", description="Optional partial symbol name.", default=""),
            "kind": schema_property(type="string", description="Symbol kind: class, function, method, type, import.", default=""),
            "parent": schema_property(type="string", description="Filter by parent scope (e.g. class name to find its methods).", default=""),
            "limit": schema_property(type="integer", description="Maximum symbols returned.", default=100),
        },
        required=[],
        handler=_handler_symbols,
    ),
    _tool(
        name="codebase_read",
        summary="Read a bounded line range from a file in a registered code repository.",
        properties={
            "rel_path": schema_property(type="string", description="Repository-relative file path."),
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "start_line": schema_property(type="integer", description="1-based inclusive start line."),
            "end_line": schema_property(type="integer", description="1-based inclusive end line; capped to 500 lines."),
        },
        required=["rel_path"],
        handler=_handler_read,
    ),
    _tool(
        name="codebase_references",
        summary="Find all references to a symbol across the repository using word-boundary search, with definition cross-check.",
        properties={
            "symbol": schema_property(type="string", description="Exact symbol name to find references for."),
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "exclude_definition": schema_property(type="boolean", description="Exclude definition sites from results.", default=True),
            "limit": schema_property(type="integer", description="Maximum references returned.", default=50),
        },
        required=["symbol"],
        handler=_handler_references,
    ),
    _tool(
        name="codebase_dependencies",
        summary="Analyze import dependencies of a file or module: upstream (what it imports) and downstream (who imports it).",
        properties={
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "rel_path": schema_property(type="string", description="Repository-relative file path to analyze.", default=""),
            "module": schema_property(type="string", description="Dotted module name to search for importers.", default=""),
            "direction": schema_property(type="string", description="'upstream', 'downstream', or 'both'.", default="both"),
            "limit": schema_property(type="integer", description="Maximum imports returned per direction.", default=100),
        },
        required=[],
        handler=_handler_dependencies,
    ),
    _tool(
        name="codebase_context",
        summary="Assemble relevant context for understanding or modifying a code region: file structure, imports, references, and importers.",
        properties={
            "rel_path": schema_property(type="string", description="Repository-relative file path to analyze."),
            "repository": _REPOSITORY_PROPERTY,
            "change_id": _CHANGE_PROPERTY,
            "focus_symbol": schema_property(type="string", description="Optional symbol to trace references for.", default=""),
            "max_symbols": schema_property(type="integer", description="Maximum symbols shown in file structure.", default=20),
        },
        required=["rel_path"],
        handler=_handler_context,
    ),
]


__all__ = ["TOOLS"]
