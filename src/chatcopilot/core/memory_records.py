"""Protected, conversation-scoped memory records and bounded local recall."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from chatcopilot.contracts.persistent_state import (
    MEMORY_MAX_ITEM_CHARS,
    MEMORY_SECTIONS,
    MemoryAppendReceipt,
    MemoryRecord,
)
from chatcopilot.core.memory_policy import evaluate_memory_content

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    item_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    section TEXT NOT NULL,
    source_actor TEXT NOT NULL,
    source_turn TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    supersedes_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memory_items_active
ON memory_items(status, updated_at DESC);
"""
_COLUMNS = (
    "item_id", "text", "section", "source_actor", "source_turn", "origin",
    "created_at", "updated_at", "version", "status", "supersedes_id",
)
_MAX_ITEMS = 1000
_MAX_CONTEXT_CHARS = 4000
_WORD_RE = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(**{key: row[key] for key in _COLUMNS})


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _bigrams(text: str) -> set[str]:
    return {
        word[index : index + 2]
        for word in _WORD_RE.findall(_normalized(text))
        for index in range(len(word) - 1)
    }


def _score(query: str, candidate: str) -> float:
    q = _normalized(query)
    c = _normalized(candidate)
    if not q:
        return 1.0
    if q in c:
        return 100.0 + len(q) / max(len(c), 1)
    words = _WORD_RE.findall(q)
    exact = sum(1 for word in words if word in c)
    pairs = _bigrams(q)
    overlap = len(pairs & _bigrams(c)) / len(pairs) if pairs else 0.0
    return exact * 10.0 + overlap * 5.0


class MemoryRecordStore:
    """A single trusted conversation's database; the caller never selects its path."""

    def __init__(self, state: Any) -> None:
        self._state = state
        self._path = state._memory_db_path()

    @contextmanager
    def _connection(self, *, write: bool) -> Iterator[sqlite3.Connection | None]:
        path = self._path
        try:
            path.lstat()
            exists = True
        except FileNotFoundError:
            exists = False
        if not exists and not write:
            yield None
            return
        if write:
            self._state._ensure_state_parent(path.parent)
        else:
            self._state._validate_state_parents(path.parent)
        with self._state._locked(path):
            try:
                path.lstat()
                exists = True
            except FileNotFoundError:
                exists = False
            if not exists:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(path, flags, 0o600)
                except OSError as exc:
                    raise RuntimeError("记忆存储无法安全创建") from exc
                os.close(fd)
            self._state._validate_file(path.lstat())
            try:
                conn = sqlite3.connect(path, timeout=5, isolation_level=None)
            except sqlite3.Error as exc:
                raise RuntimeError("记忆存储不可用") from exc
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("PRAGMA max_page_count=4096")
                if write:
                    conn.executescript(_SCHEMA)
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if write:
                    conn.commit()
            except sqlite3.Error as exc:
                if write:
                    conn.rollback()
                raise RuntimeError("记忆存储不可用") from exc
            except BaseException:
                if write:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def _active(self) -> tuple[MemoryRecord, ...]:
        with self._connection(write=False) as conn:
            if conn is None:
                return ()
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE status='active' "
                "ORDER BY updated_at DESC, item_id DESC LIMIT ?",
                (_MAX_ITEMS + 1,),
            ).fetchall()
        if len(rows) > _MAX_ITEMS:
            raise ValueError("当前作用域记忆条目超过上限，请由 Owner 整理")
        return tuple(_record(row) for row in rows)

    def snapshot(self) -> str:
        records = self._active()
        if not records:
            return ""
        sections: list[str] = ["# Memory"]
        for section in MEMORY_SECTIONS:
            entries = [item for item in reversed(records) if item.section == section]
            if entries:
                sections.extend(("", f"## {section}"))
                for item in entries:
                    timestamp = item.created_at[:16].replace("T", " ")
                    sections.append(f"- {timestamp} {item.text}")
        return "\n".join(sections) + "\n"

    def search(self, query: str, *, limit: int = 5) -> tuple[MemoryRecord, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("limit 必须在 1 到 20 之间")
        scored = [(_score(query, item.text), item) for item in self._active()]
        matching = [pair for pair in scored if pair[0] > 0]
        matching.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        return tuple(item for _score_value, item in matching[:limit])

    def context(self, query: str = "") -> str:
        active = self._active()
        pinned = [
            item for item in active
            if item.section == "decisions"
            or (item.section == "facts" and re.search(r"偏好|习惯|默认|喜欢", item.text))
        ]
        selected: list[MemoryRecord] = pinned[:4]
        if query.strip():
            seen = {item.item_id for item in selected}
            for item in self.search(query, limit=5):
                if item.item_id not in seen:
                    selected.append(item)
                    seen.add(item.item_id)
        lines = [f"- [{item.item_id}] {item.text}" for item in selected]
        output: list[str] = []
        remaining = _MAX_CONTEXT_CHARS
        for line in lines:
            if len(line) + 1 > remaining:
                break
            output.append(line)
            remaining -= len(line) + 1
        return "\n".join(output)

    def read(self, item_id: str) -> MemoryRecord | None:
        with self._connection(write=False) as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM memory_items WHERE item_id=?", (item_id,)
            ).fetchone()
            return _record(row) if row is not None else None

    def append(
        self,
        *,
        text: str,
        section: str,
        source_actor: str,
        source_turn: str,
        origin: str,
        supersedes_id: str = "",
    ) -> MemoryAppendReceipt:
        text = (text or "").strip()
        section = (section or "facts").strip() or "facts"
        if not text or len(text) > MEMORY_MAX_ITEM_CHARS:
            raise ValueError("记忆条目为空或超过单条长度上限")
        if section not in MEMORY_SECTIONS:
            raise ValueError(f"section 只能是 {', '.join(MEMORY_SECTIONS)}")
        if origin not in {"explicit", "automatic"}:
            raise ValueError("无效记忆来源")
        decision = evaluate_memory_content(text, scope=self._state.memory_scope)
        if not decision.allowed:
            raise ValueError(decision.reason)
        with self._connection(write=True) as conn:
            assert conn is not None
            existing = conn.execute(
                "SELECT item_id, version FROM memory_items WHERE status='active' AND text=?",
                (text,),
            ).fetchone()
            if existing is None and origin == "automatic" and source_turn:
                existing = conn.execute(
                    "SELECT item_id, version FROM memory_items "
                    "WHERE source_turn=? AND text=? ORDER BY created_at DESC LIMIT 1",
                    (source_turn, text),
                ).fetchone()
            if existing is not None:
                if supersedes_id and existing["item_id"] != supersedes_id:
                    changed = conn.execute(
                        "UPDATE memory_items SET status='superseded', updated_at=? "
                        "WHERE item_id=? AND status='active'",
                        (_now(), supersedes_id),
                    ).rowcount
                    if changed != 1:
                        raise ValueError("被替换的记忆不存在或已失效")
                return MemoryAppendReceipt(
                    created=False,
                    scope=self._state.memory_scope,
                    item_id=existing["item_id"],
                    version=existing["version"],
                    content_sha256=_digest(text),
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE status='active'"
            ).fetchone()[0]
            if count >= _MAX_ITEMS:
                raise ValueError("当前作用域记忆条目已满，请由 Owner 整理")
            if supersedes_id:
                changed = conn.execute(
                    "UPDATE memory_items SET status='superseded', updated_at=? "
                    "WHERE item_id=? AND status='active'",
                    (_now(), supersedes_id),
                ).rowcount
                if changed != 1:
                    raise ValueError("被替换的记忆不存在或已失效")
            item_id = uuid4().hex
            now = _now()
            conn.execute(
                "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id, text, section, source_actor, source_turn, origin,
                    now, now, 1, "active", supersedes_id,
                ),
            )
        return MemoryAppendReceipt(
            created=True,
            scope=self._state.memory_scope,
            item_id=item_id,
            version=1,
            content_sha256=_digest(text),
        )

    def update(
        self, item_id: str, *, text: str, expected_version: int,
        source_actor: str, source_turn: str,
    ) -> MemoryRecord:
        text = (text or "").strip()
        if not text or len(text) > MEMORY_MAX_ITEM_CHARS:
            raise ValueError("记忆条目为空或超过单条长度上限")
        decision = evaluate_memory_content(text, scope=self._state.memory_scope)
        if not decision.allowed:
            raise ValueError(decision.reason)
        with self._connection(write=True) as conn:
            assert conn is not None
            changed = conn.execute(
                "UPDATE memory_items SET text=?, source_actor=?, source_turn=?, "
                "origin='explicit', version=version+1, updated_at=? "
                "WHERE item_id=? AND version=? AND status='active'",
                (text, source_actor, source_turn, _now(), item_id, expected_version),
            ).rowcount
            if changed != 1:
                raise ValueError("记忆不存在、已失效或版本已变化")
            row = conn.execute(
                "SELECT * FROM memory_items WHERE item_id=?", (item_id,)
            ).fetchone()
            assert row is not None
            return _record(row)

    def delete(self, item_id: str, *, expected_version: int) -> bool:
        with self._connection(write=True) as conn:
            assert conn is not None
            changed = conn.execute(
                "UPDATE memory_items SET text='', status='deleted', version=version+1, "
                "updated_at=? WHERE item_id=? AND version=? AND status='active'",
                (_now(), item_id, expected_version),
            ).rowcount
            return changed == 1

    def clear(self) -> None:
        with self._connection(write=True) as conn:
            assert conn is not None
            conn.execute(
                "UPDATE memory_items SET text='', status='deleted', version=version+1, "
                "updated_at=? WHERE text!=''",
                (_now(),),
            )


__all__ = ["MemoryRecordStore"]
