"""Dev file operation tools: read, write, edit, delete, list, search.

edit_file opportunistically uses search-replace-py for fuzzy matching when that
optional Python 3.14+ library is installed. Other operations use standard
pathlib/subprocess.
"""
from __future__ import annotations

import subprocess
from typing import Any

from chatcopilot.external_tools.dev.config import get_dev_config
from chatcopilot.external_tools.dev.path_guard import (
    DevPathAccessError,
    ensure_listable,
    ensure_readable,
    ensure_writable,
)
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef

_MAX_READ_LINES = 2000
_MAX_SEARCH_RESULTS = 50


def _handle_read_file(args: dict[str, Any]) -> HandlerResult:
    config = get_dev_config()
    path_str = str(args.get("path") or "").strip()
    resolved, normalized = ensure_readable(config, path_str)

    if not resolved.is_file():
        return f"File not found: {normalized}", [], None

    start_line = int(args.get("start_line") or 1)
    end_line = args.get("end_line")

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Cannot read file: {e}", [], None

    lines = content.splitlines(keepends=True)
    total = len(lines)

    start_idx = max(0, start_line - 1)
    end_idx = int(end_line) if end_line else start_idx + _MAX_READ_LINES
    end_idx = min(end_idx, total)

    if end_idx - start_idx > _MAX_READ_LINES:
        end_idx = start_idx + _MAX_READ_LINES

    selected = lines[start_idx:end_idx]
    numbered = "".join(
        f"{start_idx + i + 1:6}|{line}" for i, line in enumerate(selected)
    )

    truncated = " (truncated)" if end_idx < total else ""
    summary = f"{normalized} [{start_idx+1}:{end_idx}/{total} lines]{truncated}"
    return summary, [numbered], None


def _handle_write_file(args: dict[str, Any]) -> HandlerResult:
    config = get_dev_config()
    path_str = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    resolved, normalized = ensure_writable(config, path_str)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"Wrote {normalized} ({lines} lines)", [normalized], None


def _handle_edit_file(args: dict[str, Any]) -> HandlerResult:
    config = get_dev_config()
    path_str = str(args.get("path") or "").strip()
    old_text = str(args.get("old_text") or "")
    new_text = str(args.get("new_text") or "")
    resolved, normalized = ensure_writable(config, path_str)

    if not resolved.is_file():
        return f"File not found: {normalized}", [], None
    if not old_text:
        return "old_text must not be empty", [], None

    content = resolved.read_text(encoding="utf-8", errors="replace")
    new_content = _apply_edit(content, old_text, new_text)

    if new_content is None:
        return (
            f"edit_file failed: old_text not found in {normalized}. "
            "Ensure old_text exactly matches existing content (check whitespace/indentation).",
            [],
            None,
        )

    resolved.write_text(new_content, encoding="utf-8")
    return f"Edited {normalized}", [normalized], None


def _apply_edit(content: str, old_text: str, new_text: str) -> str | None:
    """Apply search-replace edit with fallback strategies.

    Tries: 1) exact match  2) optional search-replace-py fuzzy  3) stripped match
    """
    if old_text in content:
        return content.replace(old_text, new_text, 1)

    try:
        from search_replace import apply_edits, EditBlock  # type: ignore[import-untyped]
        block = EditBlock(filename="<edit>", before=old_text, after=new_text)
        result = apply_edits([("<edit>", content, [block])], dry_run=False)
        if result:
            _, new_content = result[0]
            if new_content is not None:
                return new_content
    except (ImportError, Exception):
        pass

    stripped_old = old_text.strip()
    if not stripped_old:
        return None
    old_line_count = old_text.count("\n") + 1
    lines = content.splitlines(keepends=True)
    for i in range(len(lines) - old_line_count + 1):
        window = "".join(lines[i : i + old_line_count])
        if window.strip() == stripped_old:
            return "".join(lines[:i]) + new_text + "".join(lines[i + old_line_count :])

    return None


def _handle_delete_file(args: dict[str, Any]) -> HandlerResult:
    config = get_dev_config()
    path_str = str(args.get("path") or "").strip()
    resolved, normalized = ensure_writable(config, path_str)

    if not resolved.exists():
        return f"File not found: {normalized}", [], None

    resolved.unlink()
    return f"Deleted {normalized}", [], None


def _handle_list_directory(args: dict[str, Any]) -> HandlerResult:
    config = get_dev_config()
    path_str = str(args.get("path") or "").strip()
    resolved, normalized = ensure_listable(config, path_str)
    recursive = bool(args.get("recursive"))
    glob_pattern = str(args.get("glob") or "").strip()

    if not resolved.is_dir():
        return f"Not a directory: {normalized or '.'}", [], None

    entries: list[str] = []
    max_entries = 500

    if glob_pattern:
        pattern = f"**/{glob_pattern}" if recursive else glob_pattern
        for p in sorted(resolved.glob(pattern))[:max_entries]:
            rel = p.relative_to(config.repo_root)
            suffix = "/" if p.is_dir() else ""
            entries.append(f"{rel.as_posix()}{suffix}")
    elif recursive:
        for p in sorted(resolved.rglob("*"))[:max_entries]:
            if p.name.startswith(".") or any(
                part in (".git", "__pycache__", "node_modules", ".venv")
                for part in p.parts
            ):
                continue
            rel = p.relative_to(config.repo_root)
            suffix = "/" if p.is_dir() else ""
            entries.append(f"{rel.as_posix()}{suffix}")
    else:
        for p in sorted(resolved.iterdir())[:max_entries]:
            rel = p.relative_to(config.repo_root)
            suffix = "/" if p.is_dir() else ""
            entries.append(f"{rel.as_posix()}{suffix}")

    if not entries:
        return f"Empty directory: {normalized or '.'}", [], None

    truncated = f" (showing first {max_entries})" if len(entries) >= max_entries else ""
    listing = "\n".join(entries)
    return f"{len(entries)} entries in {normalized or '.'}{truncated}", [listing], None


def _handle_search_content(args: dict[str, Any]) -> HandlerResult:
    config = get_dev_config()
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return "pattern is required", [], None

    search_path = str(args.get("path") or "").strip()
    glob_filter = str(args.get("glob") or "").strip()
    max_results = min(int(args.get("max_results") or _MAX_SEARCH_RESULTS), 200)

    if search_path:
        try:
            target, _ = ensure_readable(config, search_path)
        except DevPathAccessError:
            target = config.repo_root
    else:
        target = config.repo_root

    cmd = ["rg", "--no-heading", "--line-number", "--color=never", f"--max-count={max_results}"]
    if glob_filter:
        cmd.extend(["--glob", glob_filter])
    cmd.extend([pattern, str(target)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=str(config.repo_root)
        )
    except FileNotFoundError:
        cmd_grep = ["grep", "-rn", "--include", glob_filter or "*", pattern, str(target)]
        try:
            result = subprocess.run(
                cmd_grep, capture_output=True, text=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "search_content requires 'rg' (ripgrep) to be installed", [], None
    except subprocess.TimeoutExpired:
        return "Search timed out", [], None

    output = result.stdout.strip()
    if not output:
        return f"No matches for pattern: {pattern}", [], None

    lines = output.splitlines()
    rel_lines: list[str] = []
    root_str = str(config.repo_root) + "/"
    for line in lines[:max_results]:
        if line.startswith(root_str):
            line = line[len(root_str):]
        rel_lines.append(line)

    return f"{len(rel_lines)} matches for '{pattern}'", ["\n".join(rel_lines)], None


TOOLS: list[ToolDef] = [
    ToolDef(
        name="read_file",
        summary="Read a file's contents with line numbers. Supports line range selection.",
        properties={
            "path": {"type": "string", "description": "Relative path from project root"},
            "start_line": {"type": "integer", "description": "First line to read (1-based, default 1)"},
            "end_line": {"type": "integer", "description": "Last line to read (inclusive, default start+2000)"},
        },
        required=["path"],
        handler=_handle_read_file,
        category="dev.files",
        owner="dev",
        weight="light",
        artifact_kinds=(),
    ),
    ToolDef(
        name="write_file",
        summary="Create or overwrite a file with the given content.",
        properties={
            "path": {"type": "string", "description": "Relative path from project root"},
            "content": {"type": "string", "description": "Full file content to write"},
        },
        required=["path", "content"],
        handler=_handle_write_file,
        category="dev.files",
        owner="dev",
        requires_role="owner",
        artifact_kinds=("file",),
    ),
    ToolDef(
        name="edit_file",
        summary=(
            "Edit a file by replacing old_text with new_text. Uses fuzzy matching "
            "to handle minor whitespace/indentation differences."
        ),
        properties={
            "path": {"type": "string", "description": "Relative path from project root"},
            "old_text": {"type": "string", "description": "Existing text to find (must be unique enough to match)"},
            "new_text": {"type": "string", "description": "Replacement text"},
        },
        required=["path", "old_text", "new_text"],
        handler=_handle_edit_file,
        category="dev.files",
        owner="dev",
        requires_role="owner",
        artifact_kinds=("file",),
        metadata={"execution_boundary": "codex"},
    ),
    ToolDef(
        name="delete_file",
        summary="Delete a file from the project.",
        properties={
            "path": {"type": "string", "description": "Relative path from project root"},
        },
        required=["path"],
        handler=_handle_delete_file,
        category="dev.files",
        owner="dev",
        requires_role="owner",
        artifact_kinds=(),
        metadata={"execution_boundary": "codex"},
    ),
    ToolDef(
        name="list_directory",
        summary="List files and directories. Supports glob patterns and recursive listing.",
        properties={
            "path": {"type": "string", "description": "Relative directory path (empty or '.' for project root)"},
            "recursive": {"type": "boolean", "description": "List recursively (default false)"},
            "glob": {"type": "string", "description": "Glob pattern to filter (e.g. '*.py', '**/*.yaml')"},
        },
        required=[],
        handler=_handle_list_directory,
        category="dev.files",
        owner="dev",
        weight="light",
        artifact_kinds=(),
    ),
    ToolDef(
        name="search_content",
        summary="Search file contents using regex pattern (ripgrep). Returns matching lines with file paths and line numbers.",
        properties={
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Subdirectory to search in (default: project root)"},
            "glob": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
            "max_results": {"type": "integer", "description": "Maximum matches to return (default 50, max 200)"},
        },
        required=["pattern"],
        handler=_handle_search_content,
        category="dev.files",
        owner="dev",
        weight="light",
        artifact_kinds=(),
    ),
]
