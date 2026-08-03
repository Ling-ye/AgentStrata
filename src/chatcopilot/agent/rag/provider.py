"""RAG retrieval provider interfaces and local text implementation."""
from __future__ import annotations

import fnmatch
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from chatcopilot.contracts.runtime import RagSourceConfig
from chatcopilot.core.wiki import WikiStore

_TEXT_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json"}
_MAX_SNIPPET_CHARS = 900
_DEFAULT_TOP_K = 4
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class RagHit:
    source: str
    chunk_id: int
    text: str
    score: float
    page_id: str = ""
    heading: str = ""
    source_refs: tuple[str, ...] = ()


class Retriever(Protocol):
    def search(self, query: str, *, top_k: int = _DEFAULT_TOP_K) -> list[RagHit]:
        """返回与 query 相关的若干文本片段，供 agent 注入上下文。"""


@dataclass(frozen=True)
class _Chunk:
    source: str
    chunk_id: int
    text: str
    tokens: frozenset[str]


class LocalTextRetriever:
    """Small local-document retriever backed by BotSpec-declared sources."""

    def __init__(self, sources: Sequence[RagSourceConfig]) -> None:
        self._sources = tuple(sources)
        self._chunks: tuple[_Chunk, ...] | None = None
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._lock = threading.RLock()

    def search(self, query: str, *, top_k: int = _DEFAULT_TOP_K) -> list[RagHit]:
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        scored: list[tuple[float, _Chunk]] = []
        for chunk in self._load_chunks():
            score = _score(query, query_tokens, chunk)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].source, item[1].chunk_id))
        return [
            RagHit(
                source=chunk.source,
                chunk_id=chunk.chunk_id,
                text=_clip(chunk.text, _MAX_SNIPPET_CHARS),
                score=score,
            )
            for score, chunk in scored[: max(0, top_k)]
        ]

    def _load_chunks(self) -> tuple[_Chunk, ...]:
        signature = _sources_signature(self._sources)
        with self._lock:
            if self._chunks is None or signature != self._signature:
                chunks: list[_Chunk] = []
                for source in self._sources:
                    chunks.extend(_load_source_chunks(source))
                self._chunks = tuple(chunks)
                self._signature = signature
            return self._chunks


class WikiRetriever:
    """Adapt the canonical Wiki store to the Agent Retriever protocol."""

    def __init__(self, store: WikiStore, *, label: str = "wiki") -> None:
        self._store = store
        self._label = label.strip() or "wiki"

    def search(self, query: str, *, top_k: int = _DEFAULT_TOP_K) -> list[RagHit]:
        return [
            RagHit(
                source=f"{self._label}/{hit.path}",
                chunk_id=hit.chunk_id,
                text=hit.text,
                score=hit.score,
                page_id=hit.page_id,
                heading=hit.heading,
                source_refs=hit.source_refs,
            )
            for hit in self._store.search(query, top_k=top_k)
        ]


class CompositeRetriever:
    """Merge several authorized retrievers into one deterministic result set."""

    def __init__(self, retrievers: Sequence[Retriever]) -> None:
        self._retrievers = tuple(retriever for retriever in retrievers if retriever is not None)

    def search(self, query: str, *, top_k: int = _DEFAULT_TOP_K) -> list[RagHit]:
        merged: dict[tuple[str, str, int], RagHit] = {}
        for retriever in self._retrievers:
            for hit in retriever.search(query, top_k=top_k):
                key = (hit.source, hit.heading, hit.chunk_id)
                current = merged.get(key)
                if current is None or hit.score > current.score:
                    merged[key] = hit
        return sorted(
            merged.values(),
            key=lambda hit: (-hit.score, hit.source, hit.heading, hit.chunk_id),
        )[: max(0, top_k)]


def render_rag_snippet(hits: Sequence[RagHit]) -> str:
    """Render retrieval hits as a compact prompt appendix."""

    if not hits:
        return ""
    lines = [
        "## 相关知识库片段",
        "以下片段来自本机器人 BotSpec 声明的本地 RAG 知识源；它们不是联网搜索结果，也不代表最新公开信息。回答时只在相关时引用。",
    ]
    for idx, hit in enumerate(hits, start=1):
        text = " ".join(hit.text.split())
        if hit.page_id or hit.heading:
            details = [f"页面: {hit.source}"]
            if hit.page_id:
                details.append(f"页面ID: {hit.page_id}")
            if hit.heading:
                details.append(f"章节: {hit.heading}")
            if hit.source_refs:
                details.append("原始来源: " + ", ".join(hit.source_refs[:3]))
            details.append(f"chunk-{hit.chunk_id}")
            lines.append(f"{idx}. " + " · ".join(details) + f"\n{text}")
        else:
            lines.append(f"{idx}. 来源: {hit.source}#chunk-{hit.chunk_id}\n{text}")
    return "\n\n".join(lines)


def _load_source_chunks(source: RagSourceConfig) -> list[_Chunk]:
    path = source.path
    if not path.exists():
        return []
    files = [path] if path.is_file() else _iter_source_files(path, source)
    chunks: list[_Chunk] = []
    for file_path in files:
        if file_path.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        display = _display_path(file_path, source)
        for chunk_id, chunk_text in enumerate(_chunk_text(text, max_chars=source.max_chunk_chars), start=1):
            tokens = frozenset(_tokenize(f"{display}\n{chunk_text}"))
            if tokens:
                chunks.append(_Chunk(source=display, chunk_id=chunk_id, text=chunk_text, tokens=tokens))
    return chunks


def _iter_source_files(root: Path, source: RagSourceConfig) -> list[Path]:
    out: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if not _matches_any(rel, candidate.name, source.include):
            continue
        if source.exclude and _matches_any(rel, candidate.name, source.exclude):
            continue
        out.append(candidate)
    return sorted(out)


def _sources_signature(sources: Sequence[RagSourceConfig]) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    for source in sources:
        path = source.path
        files = [path] if path.is_file() else (_iter_source_files(path, source) if path.is_dir() else [])
        if not files:
            entries.append((str(path), -1, -1))
            continue
        for file_path in files:
            try:
                stat = file_path.stat()
            except OSError:
                entries.append((str(file_path), -1, -1))
                continue
            entries.append((str(file_path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def _matches_any(rel: str, name: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if fnmatch.fnmatch(rel, normalized) or fnmatch.fnmatch(name, normalized):
            return True
    return False


def _display_path(file_path: Path, source: RagSourceConfig) -> str:
    try:
        if source.path.is_dir():
            return f"{source.label}/{file_path.relative_to(source.path).as_posix()}"
        return source.label
    except ValueError:
        return file_path.name


def _chunk_text(text: str, *, max_chars: int) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    heading_blocks = re.split(r"(?m)(?=^#{1,6}\s+)", normalized)
    chunks: list[str] = []
    for block in heading_blocks:
        chunks.extend(_pack_paragraphs(block.strip(), max_chars=max_chars))
    return [chunk for chunk in chunks if chunk]


def _pack_paragraphs(text: str, *, max_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(paragraph, max_chars=max_chars))
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, *, max_chars: int) -> list[str]:
    return [text[idx : idx + max_chars].strip() for idx in range(0, len(text), max_chars) if text[idx : idx + max_chars].strip()]


def _tokenize(text: str) -> set[str]:
    normalized = text.lower()
    tokens = {match.group(0) for match in _WORD_RE.finditer(normalized) if len(match.group(0)) >= 2}
    for match in _CJK_RE.finditer(normalized):
        value = match.group(0)
        if len(value) <= 12:
            tokens.add(value)
        for size in (2, 3, 4):
            for idx in range(0, max(0, len(value) - size + 1)):
                tokens.add(value[idx : idx + size])
    return tokens


def _score(raw_query: str, query_tokens: set[str], chunk: _Chunk) -> float:
    score = 0.0
    text_lower = chunk.text.lower()
    for token in query_tokens:
        if token in chunk.tokens:
            score += 1.0
        if len(token) >= 4 and token in text_lower:
            score += 1.5
    raw = raw_query.strip().lower()
    if raw and raw in text_lower:
        score += 5.0
    return score


def _clip(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 20)].rstrip() + "\n...[truncated]"


__all__ = [
    "CompositeRetriever",
    "LocalTextRetriever",
    "RagHit",
    "Retriever",
    "WikiRetriever",
    "render_rag_snippet",
]
