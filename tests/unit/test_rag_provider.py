from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.agent.rag import LocalTextRetriever, render_rag_snippet
from chatcopilot.botspec.rag import RagSourceConfig


class LocalTextRetrieverTests(unittest.TestCase):
    def test_indexes_directory_and_filters_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            (root / "phones.md").write_text(
                "# 手机发售\n\n2026 年手机发售信息需要结合厂商公告核实。\n",
                encoding="utf-8",
            )
            (root / "draft.md").write_text("2026 手机草稿不应被检索。\n", encoding="utf-8")
            (root / "image.bin").write_bytes(b"2026 phone")
            retriever = LocalTextRetriever(
                (
                    RagSourceConfig(
                        path=root,
                        label="docs",
                        include=("*.md",),
                        exclude=("draft.md",),
                        max_chunk_chars=300,
                    ),
                )
            )

            hits = retriever.search("2026 手机 发售")

            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].source, "docs/phones.md")
            self.assertIn("厂商公告", hits[0].text)
            self.assertNotIn("草稿", hits[0].text)

    def test_indexes_single_file_and_splits_chunks(self) -> None:
        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "knowledge.txt"
            file_path.write_text(
                "Alpha release note.\n\n"
                + "Beta topic " * 60
                + "\n\nFinal paragraph about gamma.",
                encoding="utf-8",
            )
            retriever = LocalTextRetriever(
                (
                    RagSourceConfig(
                        path=file_path,
                        label="knowledge.txt",
                        max_chunk_chars=220,
                    ),
                )
            )

            hits = retriever.search("gamma", top_k=2)

            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].source, "knowledge.txt")
            self.assertGreaterEqual(hits[0].chunk_id, 1)
            self.assertIn("gamma", hits[0].text)

    def test_render_snippet_includes_sources(self) -> None:
        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "facts.md"
            file_path.write_text("Alpha fact for runtime RAG.\n", encoding="utf-8")
            retriever = LocalTextRetriever((RagSourceConfig(path=file_path, label="facts.md"),))

            snippet = render_rag_snippet(retriever.search("runtime rag"))

            self.assertIn("相关知识库片段", snippet)
            self.assertIn("facts.md#chunk-1", snippet)
            self.assertIn("不是联网搜索结果", snippet)

    def test_no_match_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "facts.md"
            file_path.write_text("Alpha fact.\n", encoding="utf-8")
            retriever = LocalTextRetriever((RagSourceConfig(path=file_path, label="facts.md"),))

            self.assertEqual(retriever.search("unrelated"), [])

    def test_refreshes_cached_chunks_after_file_change(self) -> None:
        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "facts.md"
            file_path.write_text("Alpha fact.\n", encoding="utf-8")
            retriever = LocalTextRetriever((RagSourceConfig(path=file_path, label="facts.md"),))
            self.assertTrue(retriever.search("Alpha"))

            file_path.write_text("Beta refreshed fact with more text.\n", encoding="utf-8")

            self.assertTrue(retriever.search("Beta refreshed"))
            self.assertEqual(retriever.search("Alpha"), [])


if __name__ == "__main__":
    unittest.main()
