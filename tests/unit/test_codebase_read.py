from __future__ import annotations

import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from chatcopilot.agent.tools.registry import build_tools_schema, find_spec
from chatcopilot.botspec.registry import get_tool_pack_entry, known_tool_pack_names
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import CodebaseSpec
from chatcopilot.external_tools.codebase.config import load_registry, reset_cache
from chatcopilot.external_tools.codebase.path_guard import CodebasePathAccessError


class CodebaseReadToolPackTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "codebase_read"
        self.root = fixture / "repo"
        self.registry_path = fixture / "repositories.yaml"
        self.index_path = fixture / "index.sqlite3"
        self.index_path.unlink(missing_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "CHATCOPILOT_CODEBASE_REGISTRY": str(self.registry_path),
                "TEST_CODEBASE_READ_ROOT": str(self.root.resolve()),
            },
            clear=False,
        )
        self._env.start()
        self._index_patch = mock.patch(
            "chatcopilot.external_tools.codebase.index.index_path",
            return_value=self.index_path,
        )
        self._index_patch.start()
        reset_cache()

    def tearDown(self) -> None:
        reset_cache()
        self._index_patch.stop()
        self._env.stop()
        self.index_path.unlink(missing_ok=True)

    def test_tool_pack_is_registered_and_owner_only(self) -> None:
        self.assertIn("codebase.read", known_tool_pack_names())
        entry = get_tool_pack_entry("codebase.read")
        self.assertIsNotNone(entry)
        self.assertIn("chatcopilot.external_tools.codebase.tools", entry.tool_modules)
        schema, tools = build_tools_schema(tool_packs=("codebase.read",))
        names = {item["function"]["name"] for item in schema}
        self.assertEqual(
            names,
            {
                "codebase_list_repositories",
                "codebase_map",
                "codebase_read",
                "codebase_search",
                "codebase_symbols",
                "codebase_references",
                "codebase_dependencies",
                "codebase_context",
            },
        )
        self.assertTrue(all(tool.requires_role == "owner" for tool in tools.values()))

    def test_registry_and_structure_map_hide_sensitive_files(self) -> None:
        registry = load_registry()
        self.assertEqual(tuple(registry.repositories), ("demo",))
        tool = find_spec("codebase_map", tool_packs=("codebase.read",))
        self.assertIsNotNone(tool)
        summary, _, _ = tool.handler({"repository": "demo", "depth": 4})
        self.assertIn("src/app.py", summary)
        self.assertIn("README.md", summary)
        self.assertNotIn("build/hidden.py", summary)
        self.assertNotIn("notes/hidden.py", summary)

    def test_missing_repository_root_reports_resolved_path_and_hint(self) -> None:
        missing_root = self.root.parent / "missing-repo"
        with mock.patch.dict(os.environ, {"TEST_CODEBASE_READ_ROOT": str(missing_root)}):
            reset_cache()
            listing = find_spec("codebase_list_repositories", tool_packs=("codebase.read",))
            mapping = find_spec("codebase_map", tool_packs=("codebase.read",))
            self.assertIsNotNone(listing)
            self.assertIsNotNone(mapping)

            summary, _, _ = listing.handler({"repository": "demo"})
            self.assertIn("[missing]", summary)
            self.assertIn(str(missing_root.resolve()), summary)
            with self.assertRaisesRegex(
                FileNotFoundError,
                "CHATCOPILOT_CODEBASE_DEMO_ROOT",
            ) as ctx:
                mapping.handler({"repository": "demo"})
            self.assertIn(str(missing_root.resolve()), str(ctx.exception))
        reset_cache()

    def test_search_and_read_return_repository_relative_line_evidence(self) -> None:
        search = find_spec("codebase_search", tool_packs=("codebase.read",))
        read = find_spec("codebase_read", tool_packs=("codebase.read",))
        self.assertIsNotNone(search)
        self.assertIsNotNone(read)
        search_summary, _, _ = search.handler(
            {"repository": "demo", "query": "hello codebase", "fixed_strings": True}
        )
        self.assertIn("src/app.py:2", search_summary.replace("\\", "/"))
        read_summary, _, _ = read.handler(
            {"repository": "demo", "rel_path": "src/app.py", "start_line": 1, "end_line": 2}
        )
        self.assertIn("repository=demo src/app.py", read_summary)
        self.assertIn('2 |     return "hello codebase"', read_summary)
        self.assertNotIn(str(self.root), read_summary)

    def test_read_rejects_traversal_and_sensitive_file(self) -> None:
        read = find_spec("codebase_read", tool_packs=("codebase.read",))
        self.assertIsNotNone(read)
        with self.assertRaises(CodebasePathAccessError):
            read.handler({"repository": "demo", "rel_path": "../outside.py"})
        with self.assertRaises(CodebasePathAccessError):
            read.handler({"repository": "demo", "rel_path": "build/hidden.py"})
        with self.assertRaises(CodebasePathAccessError):
            read.handler({"repository": "demo", "rel_path": "notes/hidden.py"})

    def test_search_file_glob_cannot_widen_repository_include(self) -> None:
        search = find_spec("codebase_search", tool_packs=("codebase.read",))
        self.assertIsNotNone(search)
        summary, _, _ = search.handler(
            {
                "repository": "demo",
                "query": "outside include",
                "fixed_strings": True,
                "file_glob": "*.py",
            }
        )
        self.assertIn("<no matches>", summary)

    def test_search_accepts_subdirectory_that_looks_like_an_option(self) -> None:
        search = find_spec("codebase_search", tool_packs=("codebase.read",))
        self.assertIsNotNone(search)
        summary, _, _ = search.handler(
            {
                "repository": "demo",
                "query": "dash path",
                "fixed_strings": True,
                "rel_subdir": "-dash",
            }
        )
        self.assertIn("-dash/option.py:1", summary.replace("\\", "/"))

    def test_writable_registry_requires_validation_checks(self) -> None:
        invalid = self.registry_path.with_name("invalid_writable.yaml")
        with self.assertRaisesRegex(ValueError, "must declare checks"):
            load_registry(invalid, force_reload=True)

    def test_writable_registry_requires_explicit_write_globs(self) -> None:
        invalid = self.registry_path.with_name("invalid_write_globs.yaml")
        with self.assertRaisesRegex(ValueError, "must declare write_globs"):
            load_registry(invalid, force_reload=True)

    def test_codebase_tool_pack_requires_registry(self) -> None:

        spec = load_botspec(Path("bots/lingye-copilot-qq/bot.yaml"))
        packs_with_codebase = spec.tools.packs + ("codebase.read",)
        invalid = replace(
            spec,
            tools=replace(spec.tools, packs=packs_with_codebase),
            context=replace(spec.context, codebases=CodebaseSpec()),
        )
        errors = [issue for issue in validate_botspec(invalid) if issue.level == "error"]
        self.assertTrue(any(issue.field == "context.codebases.registry" for issue in errors))

    def test_incremental_symbol_index_covers_python_csharp_and_go(self) -> None:
        symbols = find_spec("codebase_symbols", tool_packs=("codebase.read",))
        self.assertIsNotNone(symbols)
        summary, _, _ = symbols.handler({"repository": "demo", "limit": 100})
        self.assertIn("hello [function] src/app.py:1", summary)
        self.assertIn("Worker [class] src/Worker.cs:3", summary)
        self.assertIn("Run [method] src/Worker.cs:5", summary)
        self.assertIn("WorkerState [type] src/worker.go:2", summary)
        self.assertIn("RunWorker [function] src/worker.go:4", summary)
        second, _, _ = symbols.handler({"repository": "demo", "query": "Worker", "limit": 20})
        self.assertIn("updated=0", second)


if __name__ == "__main__":
    unittest.main()
