from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import mock

from chatcopilot.external_tools.dev.lifecycle_job import (
    CHANGED_FILES_FILENAME,
    JOBS_DIRNAME,
    NOTIFICATION_FILENAME,
    REQUEST_FILENAME,
    STATUS_FILENAME,
    run_detached_job,
)
from chatcopilot.external_tools.dev.lifecycle_worker import main as lifecycle_worker_main
from chatcopilot.external_tools.dev.lifecycle_tools import (
    execute_finalize_self_update_from_workspace,
)
from chatcopilot.external_tools.dev.self_update_publisher import (
    SelfUpdatePublishRequest,
    publish_self_update,
)


class _FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.chat_kind = "group"
        self.chat_id = "chat-1"
        self.user_id = "user-1"
        self.user_name = "tester"


class DevLifecycleToolTests(unittest.TestCase):
    def test_finalize_self_update_writes_job_files_and_schedules_systemd(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            workspace = _FakeWorkspace(root / "workspace")
            source.mkdir()
            runtime.mkdir()
            workspace.root.mkdir()
            bot_spec = source / "bots" / "demo" / "bot.yaml"
            bot_spec.parent.mkdir(parents=True)
            bot_spec.write_text("id: demo\n", encoding="utf-8")
            env = {
                "CHATCOPILOT_DEV_ROOT": str(source),
                "CHATCOPILOT_RUNTIME_ROOT": str(runtime),
                "CHATCOPILOT_INSTANCE_ID": "demo",
                "CHATCOPILOT_SOURCE_BOT_SPEC": str(bot_spec),
                "CHATCOPILOT_SESSION_ID": "sid-1",
            }
            completed = SimpleNamespace(returncode=0, stdout="queued", stderr="")

            with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.shutil.which", return_value="/usr/bin/tool"), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.run_checks", return_value=["git diff --check"]), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.changed_files", return_value=["src/app.py"]), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.subprocess.run", return_value=completed) as run_mock:
                result = execute_finalize_self_update_from_workspace(
                    {"reason": "internal publication"},
                    workspace_payload={"root": str(workspace.root)},
                    session_id="sid-1",
                )

            self.assertTrue(result.ok)
            self.assertIsNone(result.error)
            self.assertIn("job_id:", result.summary)
            self.assertEqual(len(result.outputs), 1)
            job_dir = Path(result.outputs[0])
            self.assertEqual(job_dir.parent, workspace.root / JOBS_DIRNAME)
            request = json.loads((job_dir / REQUEST_FILENAME).read_text(encoding="utf-8"))
            status = json.loads((job_dir / STATUS_FILENAME).read_text(encoding="utf-8"))
            notification = json.loads((job_dir / NOTIFICATION_FILENAME).read_text(encoding="utf-8"))

            self.assertEqual(request["args"]["reason"], "internal publication")
            self.assertEqual(request["args"]["source_root"], str(source.resolve()))
            self.assertEqual(request["args"]["runtime_root"], str(runtime.resolve()))
            self.assertEqual(request["args"]["changed_files"], ["src/app.py"])
            self.assertEqual(request["notify"]["session_id"], "sid-1")
            self.assertEqual(status["status"], "queued")
            self.assertEqual(notification["delivery"], "pending")
            cmd = run_mock.call_args.args[0]
            self.assertEqual(cmd[0:3], ["systemd-run", "--user", "--collect"])
            self.assertIn("-m", cmd)
            self.assertIn("chatcopilot.external_tools.dev.lifecycle_worker", cmd)
            self.assertEqual(cmd[-1], str(job_dir))

    def test_publish_request_does_not_require_workspace_context(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            workspace = root / "workspace"
            source.mkdir()
            runtime.mkdir()
            workspace.mkdir()
            bot_spec = source / "bots" / "demo" / "bot.yaml"
            bot_spec.parent.mkdir(parents=True)
            bot_spec.write_text("id: demo\n", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="queued", stderr="")
            request = SelfUpdatePublishRequest(
                reason="显式发布",
                source_root=source,
                runtime_root=runtime,
                bot_spec=bot_spec,
                instance_id="demo",
                workspace_payload={
                    "root": str(workspace),
                    "chat_kind": "p2p",
                    "chat_id": "chat-1",
                    "user_id": "user-1",
                    "user_name": "tester",
                },
                session_id="sid-1",
            )

            with mock.patch("chatcopilot.external_tools.dev.self_update_publisher.shutil.which", return_value="/usr/bin/tool"), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.run_checks", return_value=["git diff --check"]), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.changed_files", return_value=["src/app.py"]), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.subprocess.run", return_value=completed):
                result = publish_self_update(request)

            self.assertIn("job_", result.job_id)
            self.assertEqual(result.job_dir.parent, workspace / JOBS_DIRNAME)
            payload = json.loads((result.job_dir / REQUEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(payload["notify"]["session_id"], "sid-1")
            self.assertEqual(payload["workspace"]["root"], str(workspace))

    def test_finalize_self_update_fails_when_systemd_run_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            workspace = _FakeWorkspace(root / "workspace")
            source.mkdir()
            runtime.mkdir()
            workspace.root.mkdir()
            bot_spec = source / "bots" / "demo" / "bot.yaml"
            bot_spec.parent.mkdir(parents=True)
            bot_spec.write_text("id: demo\n", encoding="utf-8")
            env = {
                "CHATCOPILOT_DEV_ROOT": str(source),
                "CHATCOPILOT_RUNTIME_ROOT": str(runtime),
                "CHATCOPILOT_INSTANCE_ID": "demo",
                "CHATCOPILOT_SOURCE_BOT_SPEC": str(bot_spec),
            }

            with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("chatcopilot.external_tools.dev.self_update_publisher.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "systemd-run is required"):
                    execute_finalize_self_update_from_workspace(
                        {"reason": "internal publication"},
                        workspace_payload={"root": str(workspace.root)},
                    )

    def test_changed_files_publish_freezes_an_exact_overlay(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            workspace = root / "workspace"
            (source / "src").mkdir(parents=True)
            runtime.mkdir()
            workspace.mkdir()
            changed = source / "src" / "app.py"
            changed.write_text("new value\n", encoding="utf-8")
            bot_spec = source / "bots" / "demo" / "bot.yaml"
            bot_spec.parent.mkdir(parents=True)
            bot_spec.write_text("id: demo\n", encoding="utf-8")
            request = SelfUpdatePublishRequest(
                reason="exact publish",
                source_root=source,
                runtime_root=runtime,
                bot_spec=bot_spec,
                instance_id="demo",
                workspace_payload={"root": str(workspace)},
                changed_files_override=("src/app.py", "src/deleted.py"),
                validation_root=source,
                validation_bot_spec=bot_spec,
                sync_mode="changed_files",
            )
            completed = SimpleNamespace(returncode=0, stdout="queued", stderr="")

            with mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.shutil.which",
                return_value="/usr/bin/tool",
            ), mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.run_checks",
                return_value=["git diff --check"],
            ), mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.subprocess.run",
                return_value=completed,
            ):
                result = publish_self_update(request)

            overlay = result.job_dir / "source-overlay"
            manifest = result.job_dir / CHANGED_FILES_FILENAME
            payload = json.loads((result.job_dir / REQUEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual((overlay / "src" / "app.py").read_text(encoding="utf-8"), "new value\n")
            self.assertFalse((overlay / "src" / "deleted.py").exists())
            self.assertEqual(manifest.read_text(encoding="utf-8"), "src/app.py\nsrc/deleted.py\n")
            self.assertEqual(payload["args"]["sync_mode"], "changed_files")
            self.assertEqual(payload["args"]["sync_root"], str(overlay))
            self.assertEqual(payload["args"]["changed_files_manifest"], str(manifest))

    def test_prevalidated_changed_files_abort_when_source_hash_drifts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            workspace = root / "workspace"
            (source / "src").mkdir(parents=True)
            runtime.mkdir()
            workspace.mkdir()
            changed = source / "src" / "app.py"
            changed.write_text("validated", encoding="utf-8")
            expected = hashlib.sha256(b"validated").hexdigest()
            bot_spec = source / "bots" / "demo" / "bot.yaml"
            bot_spec.parent.mkdir(parents=True)
            bot_spec.write_text("id: demo\n", encoding="utf-8")
            changed.write_text("drifted", encoding="utf-8")
            request = SelfUpdatePublishRequest(
                reason="automatic publish",
                source_root=source,
                runtime_root=runtime,
                bot_spec=bot_spec,
                instance_id="demo",
                workspace_payload={"root": str(workspace)},
                changed_files_override=("src/app.py",),
                sync_mode="changed_files",
                expected_hashes={"src/app.py": expected},
                prevalidated_checks=("full gate",),
            )

            with mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.shutil.which",
                return_value="/usr/bin/tool",
            ), mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.run_checks"
            ) as checks:
                with self.assertRaisesRegex(RuntimeError, "hash drifted"):
                    publish_self_update(request)

            checks.assert_not_called()
            self.assertFalse((workspace / JOBS_DIRNAME).exists())

    def test_prevalidated_publish_rechecks_source_after_overlay_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            workspace = root / "workspace"
            (source / "src").mkdir(parents=True)
            runtime.mkdir()
            workspace.mkdir()
            changed = source / "src" / "app.py"
            changed.write_text("validated", encoding="utf-8")
            expected = hashlib.sha256(b"validated").hexdigest()
            bot_spec = source / "bots" / "demo" / "bot.yaml"
            bot_spec.parent.mkdir(parents=True)
            bot_spec.write_text("id: demo\n", encoding="utf-8")
            request = SelfUpdatePublishRequest(
                reason="automatic publish",
                source_root=source,
                runtime_root=runtime,
                bot_spec=bot_spec,
                instance_id="demo",
                workspace_payload={"root": str(workspace)},
                changed_files_override=("src/app.py",),
                sync_mode="changed_files",
                expected_hashes={"src/app.py": expected},
                prevalidated_checks=("full gate",),
            )
            real_copy = shutil.copy2

            def copy_then_drift(src, dst):
                copied = real_copy(src, dst)
                Path(src).write_text("drifted after copy", encoding="utf-8")
                return copied

            with mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.shutil.which",
                return_value="/usr/bin/tool",
            ), mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.shutil.copy2",
                side_effect=copy_then_drift,
            ), mock.patch(
                "chatcopilot.external_tools.dev.self_update_publisher.subprocess.run"
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "hash drifted"):
                    publish_self_update(request)

            run.assert_not_called()

    def test_detached_job_passes_exact_overlay_to_update_script(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "runtime"
            overlay = root / "overlay"
            job_dir = root / "workspace" / JOBS_DIRNAME / "job_test"
            for path in (source, runtime, overlay, job_dir):
                path.mkdir(parents=True)
            update_script = source / "deploy" / "wsl" / "update_instance.sh"
            update_script.parent.mkdir(parents=True)
            update_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            manifest = job_dir / CHANGED_FILES_FILENAME
            manifest.write_text("src/app.py\n", encoding="utf-8")
            request = {
                "job_id": "job_test",
                "tool_name": "finalize_self_update",
                "execution_policy": "detached_systemd",
                "queue_name": "self_update",
                "args": {
                    "source_root": str(source),
                    "runtime_root": str(runtime),
                    "instance_id": "demo",
                    "changed_files": ["src/app.py"],
                    "sync_mode": "changed_files",
                    "sync_root": str(overlay),
                    "changed_files_manifest": str(manifest),
                },
            }
            (job_dir / REQUEST_FILENAME).write_text(json.dumps(request), encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")

            with mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job.subprocess.run",
                return_value=completed,
            ) as run, mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job._verify_service"
            ), mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job._verify_synced_files",
                return_value=["src/app.py"],
            ):
                self.assertEqual(run_detached_job(job_dir), 0)

            command = run.call_args_list[0].args[0]
            self.assertIn("--sync-src", command)
            self.assertIn(str(overlay), command)
            self.assertIn("--changed-files", command)
            self.assertIn(str(manifest), command)

    def test_detached_job_rejects_unsafe_request_before_update_subprocess(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_root = root / "workspace" / JOBS_DIRNAME
            job_dir = jobs_root / "job_unsafe_request"
            job_dir.mkdir(parents=True)
            private = root / "private-request.json"
            private.write_text(
                '{"job_id":"job_unsafe_request","tool_name":"finalize_self_update"}',
                encoding="utf-8",
            )
            (job_dir / REQUEST_FILENAME).symlink_to(private)

            with mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job.subprocess.run"
            ) as run:
                self.assertEqual(run_detached_job(job_dir), 2)

            run.assert_not_called()
            self.assertFalse((job_dir / STATUS_FILENAME).exists())
            self.assertFalse((job_dir / "result.json").exists())

            request_path = job_dir / REQUEST_FILENAME
            request_path.unlink()
            request_path.write_text(
                '{"job_id":"job_unsafe_request","tool_name":"finalize_self_update"}',
                encoding="utf-8",
            )
            request_path.chmod(0o666)
            with mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job.subprocess.run"
            ) as run:
                self.assertEqual(run_detached_job(job_dir), 2)
            run.assert_not_called()

    def test_lifecycle_worker_preserves_and_rejects_symlinked_job_ancestor(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_root = root / "workspace" / JOBS_DIRNAME
            jobs_root.mkdir(parents=True)
            external = root / "external-private-job"
            external.mkdir()
            (external / REQUEST_FILENAME).write_text(
                json.dumps(
                    {
                        "job_id": "job_symlinked_ancestor",
                        "tool_name": "finalize_self_update",
                        "execution_policy": "detached_systemd",
                        "queue_name": "self_update",
                        "args": {
                            "source_root": str(root / "source"),
                            "runtime_root": str(root / "runtime"),
                            "instance_id": "demo",
                        },
                    }
                ),
                encoding="utf-8",
            )
            job_dir = jobs_root / "job_symlinked_ancestor"
            job_dir.symlink_to(external, target_is_directory=True)

            with mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job.subprocess.run"
            ) as run:
                self.assertEqual(lifecycle_worker_main([str(job_dir)]), 2)

            run.assert_not_called()
            self.assertFalse((external / STATUS_FILENAME).exists())
            self.assertFalse((external / "result.json").exists())

    def test_detached_job_rejects_oversized_request_before_update_subprocess(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "workspace" / JOBS_DIRNAME / "job_oversized_request"
            job_dir.mkdir(parents=True)
            (job_dir / REQUEST_FILENAME).write_bytes(
                b'{"padding":"' + (b"x" * (8 * 1024 * 1024)) + b'"}'
            )

            with mock.patch(
                "chatcopilot.external_tools.dev.lifecycle_job.subprocess.run"
            ) as run:
                self.assertEqual(run_detached_job(job_dir), 2)

            run.assert_not_called()
            self.assertFalse((job_dir / STATUS_FILENAME).exists())
            self.assertFalse((job_dir / "result.json").exists())

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required")
    def test_sync_script_updates_only_manifest_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = root / "overlay"
            runtime = root / "runtime"
            (overlay / "src").mkdir(parents=True)
            (runtime / "src").mkdir(parents=True)
            (overlay / "src" / "app.py").write_text("new\n", encoding="utf-8")
            (runtime / "src" / "app.py").write_text("old\n", encoding="utf-8")
            (runtime / "src" / "deleted.py").write_text("remove\n", encoding="utf-8")
            (runtime / "src" / "unrelated.py").write_text("keep\n", encoding="utf-8")
            manifest = root / "files.txt"
            manifest.write_text("src/app.py\nsrc/deleted.py\n", encoding="utf-8")
            script = Path(__file__).resolve().parents[2] / "deploy" / "wsl" / "sync_code.sh"

            result = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--src",
                    str(overlay),
                    "--dst",
                    str(runtime),
                    "--files-from",
                    str(manifest),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((runtime / "src" / "app.py").read_text(encoding="utf-8"), "new\n")
            self.assertFalse((runtime / "src" / "deleted.py").exists())
            self.assertEqual((runtime / "src" / "unrelated.py").read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
