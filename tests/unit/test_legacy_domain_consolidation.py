from __future__ import annotations

import os
import subprocess
import sys
import time
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
        lines: list[str] = []
        result = run_codex_process(
            ["codex", "exec"],
            cwd=ROOT,
            prompt="hello",
            timeout_seconds=9,
            env={"SAFE": "1"},
            runner=runner,
            on_stdout_line=lines.append,
        )
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(lines, ["ok"])
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertEqual(runner.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(runner.call_args.kwargs["timeout"], 9)

    def test_codex_process_runner_streams_stdout_and_keeps_stderr_separate(self) -> None:
        lines: list[str] = []
        result = run_codex_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "print('first', flush=True); "
                    "print(sys.stdin.read(), flush=True); "
                    "print('private diagnostic', file=sys.stderr, flush=True)"
                ),
            ],
            cwd=ROOT,
            prompt="second",
            timeout_seconds=9,
            env=dict(os.environ),
            on_stdout_line=lines.append,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["first", "second"])
        self.assertEqual(result.stdout.splitlines(), lines)
        self.assertEqual(result.stderr.strip(), "private diagnostic")

    def test_codex_process_runner_bounds_capture_without_dropping_stream_lines(self) -> None:
        line_count = 6000
        lines: list[str] = []
        result = run_codex_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    f"[print(str(i)+'-'+('x'*96), flush=True) for i in range({line_count})]; "
                    "print('e'*131072, file=sys.stderr, flush=True)"
                ),
            ],
            cwd=ROOT,
            prompt="",
            timeout_seconds=9,
            env=dict(os.environ),
            on_stdout_line=lines.append,
        )

        self.assertEqual(len(lines), line_count)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertLessEqual(len(result.stdout), 256 * 1024)
        self.assertLessEqual(len(result.stderr), 64 * 1024)
        self.assertIn("captured output truncated", result.stdout)
        self.assertIn("captured output truncated", result.stderr)

    def test_codex_process_runner_bounds_a_single_unterminated_line(self) -> None:
        lines: list[str] = []
        result = run_codex_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*(2*1024*1024))"],
            cwd=ROOT,
            prompt="",
            timeout_seconds=9,
            env=dict(os.environ),
            on_stdout_line=lines.append,
        )

        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 1024 * 1024)
        self.assertEqual(lines[0], "[stream line omitted: size limit exceeded]")
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stdout_line_truncated)
        self.assertLessEqual(len(result.stdout), 256 * 1024)

    def test_codex_process_runner_polls_while_child_is_running(self) -> None:
        polls: list[float] = []
        result = run_codex_process(
            [
                sys.executable,
                "-c",
                "import time; print('started', flush=True); time.sleep(0.18)",
            ],
            cwd=ROOT,
            prompt="",
            timeout_seconds=9,
            env=dict(os.environ),
            on_stdout_line=lambda _line: None,
            on_poll=lambda: polls.append(time.monotonic()),
        )

        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(len(polls), 3)

    def test_codex_process_runner_serializes_stream_and_poll_callbacks(self) -> None:
        active = 0
        peak = 0

        def observed_callback() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.005)
            active -= 1

        result = run_codex_process(
            [
                sys.executable,
                "-c",
                "import time; [(print(i, flush=True), time.sleep(.01)) for i in range(12)]",
            ],
            cwd=ROOT,
            prompt="",
            timeout_seconds=9,
            env=dict(os.environ),
            on_stdout_line=lambda _line: observed_callback(),
            on_poll=observed_callback,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(peak, 1)

    @unittest.skipUnless(os.name == "posix", "process liveness assertion is POSIX-specific")
    def test_codex_process_runner_slow_poll_cannot_bypass_wall_timeout(self) -> None:
        with TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "process.pid"
            callback_started = False

            def slow_poll() -> None:
                nonlocal callback_started
                callback_started = True
                time.sleep(1.5)

            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_codex_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,pathlib,sys,time; "
                            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                            "time.sleep(30)"
                        ),
                        str(pid_path),
                    ],
                    cwd=ROOT,
                    prompt="",
                    timeout_seconds=1,
                    env=dict(os.environ),
                    on_stdout_line=lambda _line: None,
                    on_poll=slow_poll,
                )
            elapsed = time.monotonic() - started

            self.assertTrue(callback_started)
            self.assertLess(elapsed, 2.5)
            process_pid = int(pid_path.read_text(encoding="ascii"))
            self.assertFalse(Path(f"/proc/{process_pid}").exists())

    @unittest.skipUnless(os.name == "posix", "process liveness assertion is POSIX-specific")
    def test_codex_process_runner_reraises_callback_error_after_killing_process(self) -> None:
        with TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "process.pid"

            def reject_event(_line: str) -> None:
                raise RuntimeError("event sink rejected the line")

            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "event sink rejected"):
                run_codex_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,pathlib,sys,time; "
                            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                            "print('event', flush=True); time.sleep(30)"
                        ),
                        str(pid_path),
                    ],
                    cwd=ROOT,
                    prompt="",
                    timeout_seconds=9,
                    env=dict(os.environ),
                    on_stdout_line=reject_event,
                )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2.0)
            process_pid = int(pid_path.read_text(encoding="ascii"))
            self.assertFalse(Path(f"/proc/{process_pid}").exists())

    @unittest.skipUnless(os.name == "posix", "process-group assertion is POSIX-specific")
    def test_codex_process_runner_timeout_kills_pipe_inheriting_descendants(self) -> None:
        lines: list[str] = []
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_codex_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys; "
                        "child=subprocess.Popen([sys.executable,'-c',"
                        "'import time; time.sleep(30)']); "
                        "print(child.pid, flush=True)"
                    ),
                ],
                cwd=ROOT,
                prompt="",
                timeout_seconds=1,
                env=dict(os.environ),
                on_stdout_line=lines.append,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(lines), 1)
        child_pid = int(lines[0])
        proc_stat = Path(f"/proc/{child_pid}/stat")
        reap_deadline = time.monotonic() + 2.0
        while proc_stat.exists() and time.monotonic() < reap_deadline:
            fields = proc_stat.read_text(encoding="ascii", errors="replace").split()
            if len(fields) > 2 and fields[2] == "Z":
                break
            time.sleep(0.02)
        if proc_stat.exists():
            fields = proc_stat.read_text(encoding="ascii", errors="replace").split()
            self.assertGreater(len(fields), 2)
            self.assertEqual(fields[2], "Z")


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
