from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from console.control.yaml_io import load_yaml_mapping_or_empty, load_yaml_or_empty

from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.external_tools.codex_cli.process_runner import run_codex_process
ROOT = Path(__file__).resolve().parents[2]


class DeterministicHelperTests(unittest.TestCase):
    def test_positive_int_env_accepts_only_positive_integers(self) -> None:
        with mock.patch.dict(os.environ, {"LIMIT": " 7 "}, clear=False):
            self.assertEqual(positive_int_from_env("LIMIT"), 7)
        for value in ("", "0", "-2", "nope"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"LIMIT": value}, clear=False
            ):
                self.assertIsNone(positive_int_from_env("LIMIT"))

    def test_yaml_reader_is_fail_soft_for_console_projection(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.yaml"
            path.write_text("x: [", encoding="utf-8")
            self.assertEqual(load_yaml_or_empty(path), {})
            self.assertEqual(load_yaml_mapping_or_empty(path), {})

    def test_yaml_mapping_reader_rejects_non_mapping(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.yaml"
            path.write_text("- one\n- two\n", encoding="utf-8")
            self.assertEqual(load_yaml_or_empty(path), ["one", "two"])
            self.assertEqual(load_yaml_mapping_or_empty(path), {})

    def test_codex_process_runner_uses_argv_without_shell(self) -> None:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(["codex"], 0, "ok", "")
        )
        result = run_codex_process(
            ["codex", "exec"],
            cwd=ROOT,
            prompt="hello",
            timeout_seconds=9,
            env={"SAFE": "1"},
            runner=runner,
        )
        self.assertEqual(result.stdout, "ok")
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertEqual(runner.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(runner.call_args.kwargs["timeout"], 9)


class RepositoryHygieneTests(unittest.TestCase):
    def test_removed_legacy_sources_are_absent(self) -> None:
        for relative in (
            "src/chatcopilot/middleware/acp/code_route.py",
            "src/chatcopilot/middleware/acp/route_orchestrator.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_python_sources_have_no_bom(self) -> None:
        from scripts.normalize_utf8 import bom_files

        self.assertEqual(bom_files(ROOT), ())

    def test_console_pages_are_lazy_loaded(self) -> None:
        source = (ROOT / "console/web/src/App.tsx").read_text(encoding="utf-8")
        self.assertIn("lazy(() => import", source)
        self.assertIn("<Suspense", source)

    def test_frontend_feature_boundaries_exist(self) -> None:
        self.assertTrue(
            (ROOT / "console/web/src/features/evals/model.ts").is_file()
        )
        self.assertTrue(
            (ROOT / "console/web/src/features/evals/evaluationApi.ts").is_file()
        )
        self.assertTrue((ROOT / "console/web/src/features/bots/tool-editor/model.ts").is_file())
        self.assertTrue(
            (ROOT / "console/web/src/features/bots/tool-editor/useBotToolEditor.ts").is_file()
        )

    def test_codex_command_service_has_narrow_module(self) -> None:
        codex = ROOT / "src/chatcopilot/external_tools/codex_cli"
        command = (codex / "command.py").read_text(encoding="utf-8")
        self.assertFalse((codex / "tools.py").exists())
        self.assertIn("def build_codex_command", command)
        self.assertNotIn("class _ApprovedPluginJob", command)

    def test_console_observability_is_not_duplicated_in_operations(self) -> None:
        control = ROOT / "console/control"
        facade = (control / "operations.py").read_text(encoding="utf-8")
        service = (control / "observability.py").read_text(encoding="utf-8")
        self.assertNotIn("def follow_log(", facade)
        self.assertIn("def follow_log(", service)


if __name__ == "__main__":
    unittest.main()
