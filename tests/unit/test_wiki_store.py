from __future__ import annotations

import time
import hashlib
from pathlib import Path

import pytest

from chatcopilot.core.wiki import WikiStore


def _upsert(store: WikiStore, *, source: str, source_ref: str = "chat:test", **kwargs):
    return store.upsert_page(
        title=str(kwargs.get("title") or "检索系统"),
        summary=str(kwargs.get("summary") or "记录检索系统的实现约束。"),
        facts=list(kwargs.get("facts") or ["本地索引必须能够重新构建。"]),
        procedures=list(kwargs.get("procedures") or ["写入 Markdown", "刷新索引"]),
        open_questions=list(kwargs.get("open_questions") or []),
        tags=list(kwargs.get("tags") or ["RAG", "Wiki"]),
        source_text=source,
        source_kind="markdown",
        source_ref=source_ref,
        target_path=str(kwargs.get("target_path") or ""),
    )


def test_upsert_creates_structured_page_snapshot_and_index(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "wiki")

    result = _upsert(store, source="# 原始记录\n\n索引可以重建。")

    assert result.action == "created"
    assert result.page.page_id.startswith("wiki_")
    assert result.index_generation == 1
    page_path = store.pages_dir / result.page.path
    text = page_path.read_text(encoding="utf-8")
    assert "## 摘要" in text
    assert "## 事实" in text
    assert "## 步骤与决策" in text
    assert "## 待确认" in text
    assert "## 来源" in text
    assert (store.root / result.source_snapshot).read_text(encoding="utf-8").startswith("# 原始记录")
    assert hashlib.sha256((store.root / result.source_snapshot).read_bytes()).hexdigest() == result.source_hash
    assert store.index_path.is_file()


def test_equal_source_hash_is_noop_without_page_rewrite(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "wiki")
    first = _upsert(store, source="完全相同的来源")
    page_path = store.pages_dir / first.page.path
    first_mtime = page_path.stat().st_mtime_ns

    second = _upsert(store, source="完全相同的来源", summary="模型生成了不同摘要")

    assert second.action == "noop"
    assert second.page.page_id == first.page.page_id
    assert page_path.stat().st_mtime_ns == first_mtime
    assert "模型生成了不同摘要" not in page_path.read_text(encoding="utf-8")


def test_changed_source_with_same_ref_updates_existing_page(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "wiki")
    first = _upsert(store, source="第一版来源", source_ref="doc:stable")

    second = _upsert(
        store,
        source="第二版来源，增加热刷新要求",
        source_ref="doc:stable",
        facts=["新页面必须立即支持热刷新检索。"],
    )

    assert second.action == "updated"
    assert second.page.path == first.page.path
    assert second.page.page_id == first.page.page_id
    assert len(second.page.source_refs) == 2
    hits = store.search("热刷新")
    assert hits
    assert hits[0].path == first.page.path
    assert hits[0].heading == "事实"


@pytest.mark.parametrize("target", ["../escape.md", "/tmp/escape.md", "notes.txt"])
def test_page_path_guard_rejects_escape_and_non_markdown(tmp_path: Path, target: str) -> None:
    store = WikiStore(tmp_path / "wiki")

    with pytest.raises(ValueError):
        _upsert(store, source=f"source for {target}", target_path=target)
    assert not store.sources_dir.exists()


def test_search_detects_external_page_edit_without_restart(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "wiki")
    created = _upsert(store, source="初始来源", facts=["最初使用词项检索。"])
    assert store.search("词项检索")
    page_path = store.pages_dir / created.page.path
    original = page_path.read_text(encoding="utf-8")
    time.sleep(0.002)
    page_path.write_text(original.replace("词项检索", "增量检索刷新"), encoding="utf-8")

    hits = store.search("增量检索刷新")

    assert hits
    assert hits[0].path == created.page.path


def test_search_rebuilds_deleted_derived_index(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "wiki")
    created = _upsert(store, source="可重建索引来源", facts=["派生索引删除后可以重建。"])
    store.index_path.unlink()

    hits = store.search("派生索引删除")

    assert store.index_path.is_file()
    assert hits[0].page_id == created.page.page_id
    assert hits[0].source_refs == ("chat:test",)
