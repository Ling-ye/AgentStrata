"""Source-first local Markdown Wiki with a rebuildable SQLite index."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence
from uuid import uuid4

from ruamel.yaml import YAML

_CANONICAL_ROOT_ENV = "CHATCOPILOT_WIKI_ROOT"
_MAX_SOURCE_CHARS = 200_000
_MAX_FIELD_CHARS = 20_000
_MAX_LIST_ITEMS = 100
_DEFAULT_MAX_CHUNK_CHARS = 1200
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PROCESS_LOCK = threading.RLock()


@dataclass(frozen=True)
class WikiPage:
    path: str
    page_id: str
    title: str
    tags: tuple[str, ...]
    updated_at: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class WikiHit:
    path: str
    page_id: str
    title: str
    heading: str
    chunk_id: int
    text: str
    score: float
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class WikiUpsertResult:
    action: str
    page: WikiPage
    source_hash: str
    source_snapshot: str
    index_generation: int
    warnings: tuple[str, ...] = ()


class WikiStore:
    """Manage one Wiki root whose Markdown pages are the source of truth."""

    def __init__(self, root: Path, *, max_chunk_chars: int = _DEFAULT_MAX_CHUNK_CHARS) -> None:
        self.root = Path(root).expanduser().resolve()
        self.pages_dir = self.root / "pages"
        self.sources_dir = self.root / "sources"
        self.assets_dir = self.root / "assets"
        self.index_dir = self.root / ".index"
        self.index_path = self.index_dir / "wiki.db"
        self.lock_path = self.index_dir / "wiki.lock"
        self.max_chunk_chars = max(200, int(max_chunk_chars))

    @classmethod
    def from_env(cls, *, max_chunk_chars: int | None = None) -> "WikiStore":
        raw = os.environ.get(_CANONICAL_ROOT_ENV, "").strip()
        if not raw:
            raise RuntimeError(
                f"Wiki 未配置；请在机器人 local.env 设置 {_CANONICAL_ROOT_ENV}"
            )
        if max_chunk_chars is None:
            try:
                max_chunk_chars = int(
                    os.environ.get("CHATCOPILOT_WIKI_MAX_CHUNK_CHARS", "")
                    or _DEFAULT_MAX_CHUNK_CHARS
                )
            except ValueError:
                max_chunk_chars = _DEFAULT_MAX_CHUNK_CHARS
        return cls(Path(raw), max_chunk_chars=max_chunk_chars)

    def ensure_layout(self) -> None:
        for path in (self.pages_dir, self.sources_dir, self.assets_dir, self.index_dir):
            path.mkdir(parents=True, exist_ok=True)

    def upsert_page(
        self,
        *,
        title: str,
        summary: str,
        facts: Sequence[str],
        procedures: Sequence[str] = (),
        open_questions: Sequence[str] = (),
        tags: Sequence[str] = (),
        source_text: str,
        source_kind: str = "chat",
        source_ref: str = "",
        target_path: str = "",
    ) -> WikiUpsertResult:
        clean_title = _single_line(title, "title", limit=300, required=True)
        clean_summary = _required_text(summary, "summary", limit=_MAX_FIELD_CHARS)
        clean_source = _required_text(source_text, "source_text", limit=_MAX_SOURCE_CHARS)
        clean_facts = _clean_list(facts, "facts", required=True)
        clean_procedures = _clean_list(procedures, "procedures")
        clean_questions = _clean_list(open_questions, "open_questions")
        clean_tags = _dedupe(_clean_list(tags, "tags", item_limit=100))
        clean_kind = _source_kind(source_kind)
        clean_ref = _single_line(source_ref, "source_ref", limit=1000, required=False)
        source_hash = hashlib.sha256(clean_source.encode("utf-8")).hexdigest()
        source_identity = clean_ref or f"sha256:{source_hash}"
        snapshot_rel = f"sources/{source_hash}.md"
        now = _utc_now()
        duplicate_page: WikiPage | None = None
        explicit_target = bool(str(target_path or "").strip())
        requested_target = self._resolve_page_path(target_path) if explicit_target else None

        self.ensure_layout()
        with self._write_lock():
            snapshot_path = self.root / snapshot_rel
            if not snapshot_path.exists():
                _atomic_write(snapshot_path, clean_source, ensure_trailing_newline=False)

            pages = self._load_all_pages()
            duplicate = _find_page_by_source_hash(pages, source_hash)
            if duplicate is not None:
                duplicate_page = _page_summary(duplicate[0], duplicate[1])
            else:
                target = requested_target
                by_ref = _find_page_by_source_ref(pages, clean_ref) if clean_ref else None
                if target is not None and by_ref is not None and target != by_ref[0]:
                    raise ValueError(
                        f"source_ref 已属于另一页面: {by_ref[0].relative_to(self.pages_dir).as_posix()}"
                    )
                if target is None and by_ref is not None:
                    target = by_ref[0]
                if target is None:
                    target = self._new_page_path(clean_title, source_hash)

                existing_meta: dict[str, Any] = {}
                if target.exists():
                    existing_meta, _ = _parse_page(target.read_text(encoding="utf-8"))
                    if not explicit_target and by_ref is None:
                        target = self._new_page_path(clean_title, source_hash, force_suffix=True)
                        existing_meta = {}

                page_id = str(existing_meta.get("id") or f"wiki_{uuid4().hex}")
                created_at = str(existing_meta.get("created_at") or now)
                source_entries = _source_entries(existing_meta)
                source_entries.append(
                    {
                        "kind": clean_kind,
                        "ref": source_identity,
                        "hash": source_hash,
                        "captured_at": now,
                        "snapshot": snapshot_rel,
                    }
                )
                metadata = {
                    "id": page_id,
                    "title": clean_title,
                    "aliases": _string_list(existing_meta.get("aliases")),
                    "tags": clean_tags,
                    "status": str(existing_meta.get("status") or "draft"),
                    "sources": source_entries,
                    "created_at": created_at,
                    "updated_at": now,
                }
                body = _render_body(
                    title=clean_title,
                    summary=clean_summary,
                    facts=clean_facts,
                    procedures=clean_procedures,
                    open_questions=clean_questions,
                    sources=source_entries,
                )
                _atomic_write(target, _render_page(metadata, body))
                action = "updated" if existing_meta else "created"

        if duplicate_page is not None:
            generation = self.refresh_index()
            return WikiUpsertResult(
                action="noop",
                page=duplicate_page,
                source_hash=source_hash,
                source_snapshot=snapshot_rel,
                index_generation=generation,
            )

        warnings: list[str] = []
        try:
            generation = self.refresh_index(force=True)
        except Exception as exc:  # noqa: BLE001 - Markdown remains authoritative.
            generation = 0
            warnings.append(f"页面已写入，但索引刷新失败: {type(exc).__name__}: {exc}")
        return WikiUpsertResult(
            action=action,
            page=_page_summary(target, metadata),
            source_hash=source_hash,
            source_snapshot=snapshot_rel,
            index_generation=generation,
            warnings=tuple(warnings),
        )

    def list_pages(self, *, limit: int = 50) -> list[WikiPage]:
        bounded = max(1, min(int(limit), 200))
        pages = [
            _page_summary(path, metadata)
            for path, metadata, _ in self._load_all_pages()
        ]
        pages.sort(key=lambda item: (item.updated_at, item.path), reverse=True)
        return pages[:bounded]

    def read_page(self, path: str) -> tuple[WikiPage, str]:
        target = self._resolve_page_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Wiki 页面不存在: {path}")
        metadata, body = _parse_page(target.read_text(encoding="utf-8"))
        return _page_summary(target, metadata), body

    def search(self, query: str, *, top_k: int = 5) -> list[WikiHit]:
        raw_query = str(query or "").strip()
        query_tokens = _tokenize(raw_query)
        if not query_tokens:
            return []
        self.refresh_index()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.path, c.chunk_id, c.heading, c.text, c.tokens_json,
                       p.page_id, p.title, p.tags_json, p.source_refs_json
                FROM chunks c JOIN pages p ON p.path = c.path
                """
            ).fetchall()
        scored: list[WikiHit] = []
        for row in rows:
            tokens = set(json.loads(row["tokens_json"]))
            tags = tuple(json.loads(row["tags_json"]))
            score = _score(
                raw_query,
                query_tokens,
                tokens=tokens,
                title=str(row["title"]),
                heading=str(row["heading"]),
                text=str(row["text"]),
                tags=tags,
            )
            if score <= 0:
                continue
            scored.append(
                WikiHit(
                    path=str(row["path"]),
                    page_id=str(row["page_id"]),
                    title=str(row["title"]),
                    heading=str(row["heading"]),
                    chunk_id=int(row["chunk_id"]),
                    text=str(row["text"]),
                    score=score,
                    source_refs=tuple(json.loads(row["source_refs_json"])),
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.path, hit.chunk_id))
        return scored[: max(0, min(int(top_k), 20))]

    def refresh_index(self, *, force: bool = False) -> int:
        self.ensure_layout()
        with self._write_lock():
            try:
                return self._refresh_index_once(force=force)
            except sqlite3.DatabaseError:
                self.index_path.unlink(missing_ok=True)
                return self._refresh_index_once(force=True)

    def _refresh_index_once(self, *, force: bool) -> int:
        signature = self._page_signature()
        with self._connect() as conn:
            self._init_schema(conn)
            current_signature = _meta_value(conn, "signature")
            current_generation = int(_meta_value(conn, "generation") or 0)
            if not force and current_signature == signature:
                return current_generation

            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM pages")
            for path, metadata, body in self._load_all_pages():
                relative = path.relative_to(self.pages_dir).as_posix()
                page_id = str(metadata.get("id") or "")
                title = str(metadata.get("title") or path.stem)
                tags = _string_list(metadata.get("tags"))
                source_refs = [str(item.get("ref") or "") for item in _source_entries(metadata)]
                conn.execute(
                    "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
                    (
                        relative,
                        page_id,
                        title,
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps([ref for ref in source_refs if ref], ensure_ascii=False),
                    ),
                )
                for chunk_id, (heading, text) in enumerate(
                    _chunk_markdown(body, max_chars=self.max_chunk_chars), start=1
                ):
                    tokens = sorted(_tokenize(f"{relative}\n{title}\n{' '.join(tags)}\n{heading}\n{text}"))
                    conn.execute(
                        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                        (relative, chunk_id, heading, text, json.dumps(tokens, ensure_ascii=False)),
                    )
            generation = current_generation + 1
            _set_meta(conn, "signature", signature)
            _set_meta(conn, "generation", str(generation))
            conn.commit()
            return generation

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
                path TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                title TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                path TEXT NOT NULL,
                chunk_id INTEGER NOT NULL,
                heading TEXT NOT NULL,
                text TEXT NOT NULL,
                tokens_json TEXT NOT NULL,
                PRIMARY KEY (path, chunk_id)
            );
            """
        )

    def _load_all_pages(self) -> list[tuple[Path, dict[str, Any], str]]:
        out: list[tuple[Path, dict[str, Any], str]] = []
        if not self.pages_dir.is_dir():
            return out
        for path in sorted(self.pages_dir.rglob("*.md")):
            if path.is_symlink():
                continue
            try:
                metadata, body = _parse_page(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append((path, metadata, body))
        return out

    def _resolve_page_path(self, raw: str) -> Path:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            raise ValueError("Wiki 页面路径不能为空")
        rel = PurePosixPath(value)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Wiki 页面路径必须位于 pages/ 内且不能包含 ..")
        if rel.parts and rel.parts[0] == "pages":
            rel = PurePosixPath(*rel.parts[1:])
        if not rel.parts:
            raise ValueError("Wiki 页面路径不能为空")
        if rel.suffix == "":
            rel = rel.with_suffix(".md")
        if rel.suffix.lower() != ".md":
            raise ValueError("Wiki 页面仅支持 .md")
        target = (self.pages_dir / Path(*rel.parts)).resolve()
        try:
            target.relative_to(self.pages_dir)
        except ValueError as exc:
            raise ValueError("Wiki 页面路径逃逸 pages/") from exc
        return target

    def _new_page_path(self, title: str, source_hash: str, *, force_suffix: bool = False) -> Path:
        slug = _slugify(title)
        candidate = self.pages_dir / f"{slug}.md"
        if force_suffix or candidate.exists():
            candidate = self.pages_dir / f"{slug}-{source_hash[:8]}.md"
        return candidate

    def _page_signature(self) -> str:
        digest = hashlib.sha256()
        if not self.pages_dir.is_dir():
            return digest.hexdigest()
        for path in sorted(self.pages_dir.rglob("*.md")):
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(path.relative_to(self.pages_dir).as_posix().encode("utf-8"))
            digest.update(f":{stat.st_mtime_ns}:{stat.st_size}".encode("ascii"))
        return digest.hexdigest()

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK:
            handle = self.lock_path.open("a+", encoding="utf-8")
            try:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                handle.close()


def _atomic_write(path: Path, text: str, *, ensure_trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if ensure_trailing_newline and text and not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _render_page(metadata: dict[str, Any], body: str) -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    stream = StringIO()
    yaml.dump(metadata, stream)
    return f"---\n{stream.getvalue().rstrip()}\n---\n\n{body.strip()}\n"


def _parse_page(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("Wiki 页面缺少 YAML frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Wiki 页面 frontmatter 未闭合")
    yaml = YAML(typ="safe")
    metadata = yaml.load(normalized[4:end]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("Wiki 页面 frontmatter 必须是 mapping")
    return dict(metadata), normalized[end + 5 :].strip()


def _render_body(
    *,
    title: str,
    summary: str,
    facts: Sequence[str],
    procedures: Sequence[str],
    open_questions: Sequence[str],
    sources: Sequence[dict[str, Any]],
) -> str:
    lines = [f"# {title}", "", "## 摘要", "", summary, "", "## 事实", ""]
    lines.extend(f"- {item}" for item in facts)
    lines.extend(["", "## 步骤与决策", ""])
    lines.extend((f"{idx}. {item}" for idx, item in enumerate(procedures, start=1)))
    if not procedures:
        lines.append("- 暂无。")
    lines.extend(["", "## 待确认", ""])
    lines.extend(f"- {item}" for item in open_questions)
    if not open_questions:
        lines.append("- 暂无。")
    lines.extend(["", "## 来源", ""])
    for item in sources:
        ref = str(item.get("ref") or "unknown")
        snapshot = str(item.get("snapshot") or "")
        digest = str(item.get("hash") or "")[:12]
        lines.append(f"- `{ref}` -> `{snapshot}` (`sha256:{digest}`)")
    return "\n".join(lines)


def _page_summary(path: Path, metadata: dict[str, Any]) -> WikiPage:
    pages_dir = next((parent for parent in path.parents if parent.name == "pages"), path.parent)
    try:
        relative = path.relative_to(pages_dir).as_posix()
    except ValueError:
        relative = path.name
    return WikiPage(
        path=relative,
        page_id=str(metadata.get("id") or ""),
        title=str(metadata.get("title") or path.stem),
        tags=tuple(_string_list(metadata.get("tags"))),
        updated_at=str(metadata.get("updated_at") or ""),
        source_refs=tuple(
            str(item.get("ref") or "") for item in _source_entries(metadata) if item.get("ref")
        ),
    )


def _find_page_by_source_hash(
    pages: Sequence[tuple[Path, dict[str, Any], str]], source_hash: str
) -> tuple[Path, dict[str, Any], str] | None:
    for page in pages:
        if any(str(item.get("hash") or "") == source_hash for item in _source_entries(page[1])):
            return page
    return None


def _find_page_by_source_ref(
    pages: Sequence[tuple[Path, dict[str, Any], str]], source_ref: str
) -> tuple[Path, dict[str, Any], str] | None:
    for page in pages:
        if any(str(item.get("ref") or "") == source_ref for item in _source_entries(page[1])):
            return page
    return None


def _source_entries(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("sources")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _chunk_markdown(text: str, *, max_chars: int) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    heading = "正文"
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        block = "\n".join(buffer).strip()
        buffer = []
        if not block:
            return
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", block) if part.strip()]
        current = ""
        for paragraph in paragraphs:
            if len(paragraph) > max_chars:
                if current:
                    chunks.append((heading, current))
                    current = ""
                for start in range(0, len(paragraph), max_chars):
                    part = paragraph[start : start + max_chars].strip()
                    if part:
                        chunks.append((heading, part))
                continue
            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if len(candidate) <= max_chars:
                current = candidate
            else:
                chunks.append((heading, current))
                current = paragraph
        if current:
            chunks.append((heading, current))

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            buffer.append(line)
        else:
            buffer.append(line)
    flush()
    return chunks


def _tokenize(text: str) -> set[str]:
    normalized = str(text or "").lower()
    tokens = {match.group(0) for match in _WORD_RE.finditer(normalized) if len(match.group(0)) >= 2}
    for match in _CJK_RE.finditer(normalized):
        value = match.group(0)
        if len(value) <= 12:
            tokens.add(value)
        for size in (2, 3, 4):
            for idx in range(0, max(0, len(value) - size + 1)):
                tokens.add(value[idx : idx + size])
    return tokens


def _score(
    raw_query: str,
    query_tokens: set[str],
    *,
    tokens: set[str],
    title: str,
    heading: str,
    text: str,
    tags: Sequence[str],
) -> float:
    title_tokens = _tokenize(title)
    heading_tokens = _tokenize(heading)
    tag_tokens = _tokenize(" ".join(tags))
    score = 0.0
    lower_text = text.lower()
    for token in query_tokens:
        if token in tokens:
            score += 1.0
        if token in title_tokens:
            score += 2.5
        if token in heading_tokens:
            score += 1.5
        if token in tag_tokens:
            score += 1.0
        if len(token) >= 4 and token in lower_text:
            score += 1.5
    raw = raw_query.lower().strip()
    if raw and raw in lower_text:
        score += 5.0
    if raw and raw in title.lower():
        score += 6.0
    return score


def _meta_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else ""


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _required_text(value: Any, name: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > limit:
        raise ValueError(f"{name} 超过长度上限 {limit}")
    return text


def _single_line(value: Any, name: str, *, limit: int, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} 不能为空")
    if any(char in text for char in ("\r", "\n", "\x00")):
        raise ValueError(f"{name} 必须是单行文本")
    if len(text) > limit:
        raise ValueError(f"{name} 超过长度上限 {limit}")
    return text


def _clean_list(
    value: Sequence[str], name: str, *, required: bool = False, item_limit: int = 2000
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} 必须是字符串数组")
    if len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"{name} 最多 {_MAX_LIST_ITEMS} 项")
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > item_limit:
            raise ValueError(f"{name} 单项超过长度上限 {item_limit}")
        out.append(text)
    if required and not out:
        raise ValueError(f"{name} 至少包含一项")
    return out


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _source_kind(value: str) -> str:
    clean = str(value or "chat").strip().lower()
    if clean not in {"chat", "text", "markdown"}:
        raise ValueError("source_kind 仅支持 chat/text/markdown")
    return clean


def _slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).strip().lower()
    chars: list[str] = []
    pending_dash = False
    for char in normalized:
        if char.isalnum() or char in {"_", "-"}:
            if pending_dash and chars and chars[-1] != "-":
                chars.append("-")
            chars.append(char)
            pending_dash = False
        else:
            pending_dash = True
    slug = "".join(chars).strip("-_")[:80].rstrip("-_")
    return slug or "page"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["WikiHit", "WikiPage", "WikiStore", "WikiUpsertResult"]
