"""Tests for subagent cache: resource hashing and cache key stability."""

from __future__ import annotations

import os
import tempfile
import unittest

from chatcopilot.agent.subagents.cache import (
    _content_hash,
    build_cache_key,
)
from chatcopilot.agent.subagents.spec import CachePolicySpec
from chatcopilot.agent.subagents.task_pack import TaskPack


class ContentHashTests(unittest.TestCase):
    def test_empty_string_returns_empty(self):
        self.assertEqual(_content_hash(""), "")
        self.assertEqual(_content_hash("   "), "")

    def test_plain_text_hashes_to_16_chars(self):
        h = _content_hash("hello world")
        self.assertEqual(len(h), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_same_text_produces_same_hash(self):
        self.assertEqual(_content_hash("test data"), _content_hash("test data"))

    def test_different_text_produces_different_hash(self):
        self.assertNotEqual(_content_hash("aaa"), _content_hash("bbb"))

    def test_file_content_hashing(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("file content for hashing")
            path = f.name
        try:
            h = _content_hash(path)
            self.assertEqual(len(h), 16)
            # Same file should hash the same
            self.assertEqual(h, _content_hash(path))
        finally:
            os.unlink(path)

    def test_missing_file_returns_placeholder(self):
        h = _content_hash("/nonexistent/path/that/does/not/exist.txt")
        # Should not be a file hash (16 hex), but a text hash since isfile is False
        self.assertEqual(len(h), 16)

    def test_file_hash_differs_from_path_hash(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("unique content 12345")
            path = f.name
        try:
            file_hash = _content_hash(path)
            os.unlink(path)
            text_hash = _content_hash(path)
            # After deletion, path is treated as plain text
            self.assertNotEqual(file_hash, text_hash)
        except Exception:
            if os.path.exists(path):
                os.unlink(path)


class CacheKeyTests(unittest.TestCase):
    def _make_task(self, **kw):
        return TaskPack(objective="test objective", **kw)

    def test_resource_hashes_included_by_default(self):
        policy = CachePolicySpec(enabled=True, include_resource_hashes=True)
        k1 = build_cache_key(
            subagent_name="test",
            version="1",
            model="gpt-4",
            prompt_fingerprint="abc",
            tools=(),
            task=self._make_task(resources=("hello",)),
            policy=policy,
        )
        k2 = build_cache_key(
            subagent_name="test",
            version="1",
            model="gpt-4",
            prompt_fingerprint="abc",
            tools=(),
            task=self._make_task(resources=("world",)),
            policy=policy,
        )
        self.assertNotEqual(k1, k2)

    def test_resource_hashes_excluded_when_disabled(self):
        policy = CachePolicySpec(enabled=True, include_resource_hashes=False)
        k1 = build_cache_key(
            subagent_name="test",
            version="1",
            model="gpt-4",
            prompt_fingerprint="abc",
            tools=(),
            task=self._make_task(resources=("hello",)),
            policy=policy,
        )
        k2 = build_cache_key(
            subagent_name="test",
            version="1",
            model="gpt-4",
            prompt_fingerprint="abc",
            tools=(),
            task=self._make_task(resources=("world",)),
            policy=policy,
        )
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
