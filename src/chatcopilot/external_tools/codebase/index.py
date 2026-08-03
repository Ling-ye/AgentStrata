"""Incremental SQLite symbol index for registered source repositories.

Schema v2 additions over v1:
- symbols: parent (scope hierarchy), end_line, docstring
- imports table: module-level import graph for dependency analysis
"""
from __future__ import annotations

import ast
import hashlib
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from chatcopilot.external_tools.codebase.config import CodeRepositoryConfig, codebase_cache_root
from chatcopilot.external_tools.codebase.fallback_search import list_visible_files
from chatcopilot.external_tools.codebase.path_guard import matches_any

_SCHEMA_VERSION = 2
_MAX_SIGNATURE_CHARS = 400
_MAX_DOCSTRING_CHARS = 200

_CS_TYPE_RE = re.compile(r"\b(class|struct|interface|enum|record)\s+([A-Za-z_]\w*)")
_CS_METHOD_RE = re.compile(
    r"\b(?:public|private|protected|internal|static|virtual|override|async|sealed|new|partial|\s)+"
    r"[A-Za-z_][\w<>,.?\[\]\s]*\s+([A-Za-z_]\w*)\s*\("
)
_GO_TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+", re.MULTILINE)
_GO_FUNC_RE = re.compile(
    r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE
)
_JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(class|function|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class IndexStats:
    repository_id: str
    files_total: int
    files_updated: int
    files_removed: int
    symbols_total: int


@dataclass(frozen=True)
class SymbolHit:
    name: str
    kind: str
    path: str
    line: int
    signature: str
    parent: str = ""
    end_line: int = 0
    docstring: str = ""


@dataclass(frozen=True)
class ImportHit:
    path: str
    module: str
    names: str
    line: int
    is_from: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def refresh_index(repository: CodeRepositoryConfig) -> IndexStats:
    if not repository.root.is_dir():
        raise FileNotFoundError(f"registered repository root is missing: {repository.repository_id}")
    db_path = index_path(repository)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    current_paths = _list_files(repository)
    updated = 0
    removed = 0
    with closing(sqlite3.connect(db_path)) as db:
        _ensure_schema(db)
        known = {
            str(row[0]): (int(row[1]), int(row[2]))
            for row in db.execute("SELECT path, mtime_ns, size FROM files")
        }
        for rel_path in current_paths:
            target = repository.root / Path(rel_path)
            try:
                stat = target.stat()
            except OSError:
                continue
            previous = known.pop(rel_path, None)
            if previous == (stat.st_mtime_ns, stat.st_size):
                continue
            _index_file(db, repository, target, rel_path, stat.st_mtime_ns, stat.st_size)
            updated += 1
        for stale_path in known:
            db.execute("DELETE FROM files WHERE path = ?", (stale_path,))
            removed += 1
        db.commit()
        symbols_total = int(db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
    return IndexStats(repository.repository_id, len(current_paths), updated, removed, symbols_total)


def search_symbols(
    repository: CodeRepositoryConfig,
    *,
    query: str = "",
    kind: str = "",
    parent: str = "",
    limit: int = 100,
) -> tuple[IndexStats, list[SymbolHit]]:
    stats = refresh_index(repository)
    clauses: list[str] = []
    values: list[object] = []
    if query.strip():
        clauses.append("name LIKE ?")
        values.append(f"%{query.strip()}%")
    if kind.strip():
        clauses.append("kind = ?")
        values.append(kind.strip())
    if parent.strip():
        clauses.append("parent LIKE ?")
        values.append(f"%{parent.strip()}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(0, min(limit, 500)))
    sql = (
        "SELECT name, kind, path, line, signature, parent, end_line, docstring FROM symbols"
        f"{where} ORDER BY name, path, line LIMIT ?"
    )
    with closing(sqlite3.connect(index_path(repository))) as db:
        hits = [SymbolHit(*row) for row in db.execute(sql, values)]
    return stats, hits


def search_imports(
    repository: CodeRepositoryConfig,
    *,
    module: str = "",
    path: str = "",
    limit: int = 200,
) -> tuple[IndexStats, list[ImportHit]]:
    """Query the import graph. Filter by target module or source file path."""
    stats = refresh_index(repository)
    clauses: list[str] = []
    values: list[object] = []
    if module.strip():
        clauses.append("module LIKE ?")
        values.append(f"%{module.strip()}%")
    if path.strip():
        clauses.append("path LIKE ?")
        values.append(f"%{path.strip()}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(0, min(limit, 1000)))
    sql = (
        "SELECT path, module, names, line, is_from FROM imports"
        f"{where} ORDER BY path, line LIMIT ?"
    )
    with closing(sqlite3.connect(index_path(repository))) as db:
        hits = [ImportHit(*row) for row in db.execute(sql, values)]
    return stats, hits


def find_references(
    repository: CodeRepositoryConfig,
    *,
    symbol_name: str,
    exclude_definition: bool = True,
    limit: int = 100,
) -> tuple[IndexStats, list[SymbolHit], list[_ReferenceHit]]:
    """Find all references to a symbol via ripgrep + definition cross-check."""
    stats = refresh_index(repository)

    # Get definitions to exclude them if requested
    definitions: set[tuple[str, int]] = set()
    if exclude_definition:
        with closing(sqlite3.connect(index_path(repository))) as db:
            for row in db.execute(
                "SELECT path, line FROM symbols WHERE name = ?", (symbol_name,)
            ):
                definitions.add((str(row[0]), int(row[1])))

    # ripgrep for occurrences as word boundary match
    hits = _rg_symbol_references(repository, symbol_name, limit=limit * 2)
    results: list[_ReferenceHit] = []
    for hit in hits:
        if exclude_definition and (hit.path, hit.line) in definitions:
            continue
        results.append(hit)
        if len(results) >= limit:
            break

    # Get symbol definitions for context
    with closing(sqlite3.connect(index_path(repository))) as db:
        defs = [
            SymbolHit(*row)
            for row in db.execute(
                "SELECT name, kind, path, line, signature, parent, end_line, docstring "
                "FROM symbols WHERE name = ? ORDER BY path, line",
                (symbol_name,),
            )
        ]
    return stats, defs, results


def index_path(repository: CodeRepositoryConfig) -> Path:
    root_key = hashlib.sha256(str(repository.root).encode("utf-8")).hexdigest()[:10]
    return codebase_cache_root() / "indexes" / f"{repository.repository_id}-{root_key}.sqlite3"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    current = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if current is None or int(current[0]) != _SCHEMA_VERSION:
        db.execute("DROP TABLE IF EXISTS imports")
        db.execute("DROP TABLE IF EXISTS symbols")
        db.execute("DROP TABLE IF EXISTS files")
    db.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        "path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, "
        "content_hash TEXT NOT NULL, language TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS symbols ("
        "path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE, "
        "name TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL, "
        "end_line INTEGER NOT NULL DEFAULT 0, "
        "signature TEXT NOT NULL, parent TEXT NOT NULL DEFAULT '', "
        "docstring TEXT NOT NULL DEFAULT '')"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS imports ("
        "path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE, "
        "module TEXT NOT NULL, names TEXT NOT NULL DEFAULT '', "
        "line INTEGER NOT NULL, is_from INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_imports_path ON imports(path)")
    db.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )


# ---------------------------------------------------------------------------
# File indexing
# ---------------------------------------------------------------------------


def _index_file(
    db: sqlite3.Connection,
    repository: CodeRepositoryConfig,
    target: Path,
    rel_path: str,
    mtime_ns: int,
    size: int,
) -> None:
    if size > repository.max_read_bytes:
        text = ""
        digest = "oversize"
    else:
        try:
            raw = target.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            return
    language = _language_for(target.suffix.lower())
    db.execute("DELETE FROM symbols WHERE path = ?", (rel_path,))
    db.execute("DELETE FROM imports WHERE path = ?", (rel_path,))
    db.execute(
        "INSERT OR REPLACE INTO files(path, mtime_ns, size, content_hash, language) "
        "VALUES(?, ?, ?, ?, ?)",
        (rel_path, mtime_ns, size, digest, language),
    )
    symbols, imports = _extract_all(text, language)
    for sym in symbols:
        db.execute(
            "INSERT INTO symbols(path, name, kind, line, end_line, signature, parent, docstring) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (rel_path, *sym),
        )
    for imp in imports:
        db.execute(
            "INSERT INTO imports(path, module, names, line, is_from) VALUES(?, ?, ?, ?, ?)",
            (rel_path, *imp),
        )


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

# Each symbol tuple: (name, kind, line, end_line, signature, parent, docstring)
_SymbolTuple = tuple[str, str, int, int, str, str, str]
# Each import tuple: (module, names, line, is_from)
_ImportTuple = tuple[str, str, int, int]


def _extract_all(text: str, language: str) -> tuple[list[_SymbolTuple], list[_ImportTuple]]:
    if not text:
        return [], []
    if language == "python":
        return _python_extract(text)
    symbols = _extract_symbols_legacy(text, language)
    return symbols, []


def _extract_symbols_legacy(text: str, language: str) -> list[_SymbolTuple]:
    """Non-Python languages: regex-based extraction (no parent/docstring)."""
    patterns: tuple[tuple[re.Pattern[str], str], ...] = ()
    if language == "go":
        patterns = ((_GO_TYPE_RE, "type"), (_GO_FUNC_RE, "function"))
    elif language == "csharp":
        patterns = ((_CS_TYPE_RE, "type"), (_CS_METHOD_RE, "method"))

    if patterns:
        out: list[_SymbolTuple] = []
        for pattern, default_kind in patterns:
            for match in pattern.finditer(text):
                if language == "csharp" and pattern is _CS_TYPE_RE:
                    kind, name = match.group(1), match.group(2)
                else:
                    kind, name = default_kind, match.group(1)
                line = text.count("\n", 0, match.start()) + 1
                sig = match.group(0).strip()[:_MAX_SIGNATURE_CHARS]
                out.append((name, kind, line, 0, sig, "", ""))
        return out

    if language in {"javascript", "typescript"}:
        return [
            (
                match.group(2),
                match.group(1),
                text.count("\n", 0, match.start()) + 1,
                0,
                match.group(0).strip()[:_MAX_SIGNATURE_CHARS],
                "",
                "",
            )
            for match in _JS_SYMBOL_RE.finditer(text)
        ]
    return []


def _python_extract(text: str) -> tuple[list[_SymbolTuple], list[_ImportTuple]]:
    """Full Python extraction with scope hierarchy, docstrings, and imports."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []

    symbols: list[_SymbolTuple] = []
    imports: list[_ImportTuple] = []

    # Build parent map via walking with explicit stack
    _walk_python_tree(tree, text, symbols, imports, parent_name="")
    return symbols, imports


def _walk_python_tree(
    node: ast.AST,
    text: str,
    symbols: list[_SymbolTuple],
    imports: list[_ImportTuple],
    parent_name: str,
) -> None:
    """Recursively walk AST, tracking parent scope names."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            sig = _python_signature(child, text)
            doc = _python_docstring(child)
            end = getattr(child, "end_lineno", child.lineno) or child.lineno
            symbols.append((child.name, "class", child.lineno, end, sig, parent_name, doc))
            # Recurse into class body with class name as parent
            qualified = f"{parent_name}.{child.name}" if parent_name else child.name
            _walk_python_tree(child, text, symbols, imports, parent_name=qualified)

        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if parent_name else "function"
            sig = _python_signature(child, text)
            doc = _python_docstring(child)
            end = getattr(child, "end_lineno", child.lineno) or child.lineno
            symbols.append((child.name, kind, child.lineno, end, sig, parent_name, doc))
            # Recurse into function body (nested classes/functions)
            qualified = f"{parent_name}.{child.name}" if parent_name else child.name
            _walk_python_tree(child, text, symbols, imports, parent_name=qualified)

        elif isinstance(child, ast.Import):
            for alias in child.names:
                imports.append((alias.name, alias.asname or "", child.lineno, 0))
                symbols.append((
                    alias.name, "import", child.lineno, child.lineno,
                    f"import {alias.name}", parent_name, "",
                ))

        elif isinstance(child, ast.ImportFrom):
            module = child.module or ""
            names_str = ", ".join(
                (a.asname or a.name) for a in child.names
            )[:200]
            imports.append((module, names_str, child.lineno, 1))
            symbols.append((
                module, "import", child.lineno, child.lineno,
                f"from {module} import {names_str}"[:_MAX_SIGNATURE_CHARS],
                parent_name, "",
            ))

        else:
            # Continue walking into other compound statements (if/for/with/try)
            _walk_python_tree(child, text, symbols, imports, parent_name=parent_name)


def _python_signature(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, text: str) -> str:
    """Extract the full signature line(s) including decorators."""
    segment = ast.get_source_segment(text, node)
    if segment:
        # Take up to the colon ending the def/class line
        lines = segment.splitlines()
        sig_lines: list[str] = []
        for line in lines:
            sig_lines.append(line)
            if line.rstrip().endswith(":"):
                break
        return "\n".join(sig_lines)[:_MAX_SIGNATURE_CHARS]
    return node.name


def _python_docstring(node: ast.AST) -> str:
    """Extract the leading docstring from a class/function."""
    body = getattr(node, "body", None)
    if not body:
        return ""
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        val = first.value.value
        if isinstance(val, str):
            return val.strip()[:_MAX_DOCSTRING_CHARS]
    return ""


# ---------------------------------------------------------------------------
# Reference search via ripgrep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReferenceHit:
    path: str
    line: int
    content: str


def _rg_symbol_references(
    repository: CodeRepositoryConfig,
    symbol: str,
    *,
    limit: int = 200,
) -> list[_ReferenceHit]:
    """Use ripgrep with word-boundary matching to find symbol references."""
    executable = shutil.which("rg")
    if not executable:
        return []
    cmd = _base_rg_command(repository, executable)
    cmd += [
        "--no-heading", "--with-filename", "--line-number", "--color=never",
        "-w", "-e", symbol, "--", ".",
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(repository.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if completed.returncode not in (0, 1):
        return []
    results: list[_ReferenceHit] = []
    for raw_line in completed.stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path = parts[0].replace("\\", "/").removeprefix("./")
        if not _visible_path(repository, file_path):
            continue
        try:
            line_no = int(parts[1])
        except ValueError:
            continue
        results.append(_ReferenceHit(path=file_path, line=line_no, content=parts[2].strip()[:200]))
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _language_for(suffix: str) -> str:
    return {
        ".py": "python", ".go": "go", ".cs": "csharp", ".js": "javascript",
        ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    }.get(suffix, "text")


def _list_files(repository: CodeRepositoryConfig) -> list[str]:
    executable = shutil.which("rg")
    if not executable:
        return list_visible_files(repository)
    cmd = [executable, "--files", "--color=never"]
    for pattern in repository.deny_globs:
        cmd += ["--glob", f"!{pattern}"]
    if repository.allow_extensions:
        extensions = ",".join(ext.lstrip(".") for ext in repository.allow_extensions)
        cmd += ["--glob", f"*.{{{extensions}}}"]
    completed = subprocess.run(
        cmd,
        cwd=str(repository.root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"ripgrep --files failed: {completed.stderr.strip()}")
    visible: list[str] = []
    for line in completed.stdout.splitlines():
        rel_path = line.replace("\\", "/").removeprefix("./")
        if not rel_path or matches_any(rel_path, repository.deny_globs):
            continue
        if repository.include_globs and not matches_any(rel_path, repository.include_globs):
            continue
        if repository.allow_extensions and Path(rel_path).suffix.lower() not in repository.allow_extensions:
            continue
        visible.append(rel_path)
    return sorted(visible)


def _base_rg_command(repository: CodeRepositoryConfig, executable: str | None = None) -> list[str]:
    executable = executable or shutil.which("rg")
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


__all__ = [
    "ImportHit",
    "IndexStats",
    "SymbolHit",
    "find_references",
    "index_path",
    "refresh_index",
    "search_imports",
    "search_symbols",
]
