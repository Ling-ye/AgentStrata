from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import yaml

from chatcopilot.external_tools.codebase.config import reset_cache
from chatcopilot.external_tools.repository_tasks import RepositoryTaskService

class RepositoryTaskLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = Path(tempfile.gettempdir()) / f"chatcopilot-codebase-change-{uuid.uuid4().hex}"
        cls.runtime.mkdir(parents=True, exist_ok=False)
        cls.remote = cls.runtime / "remote.git"
        cls.seed = cls.runtime / "seed"
        cls.cache = cls.runtime / "cache"
        cls.registry = cls.runtime / "repositories.yaml"
        cls._run("git", "init", "--bare", str(cls.remote))
        cls._run("git", "init", "-b", "main", str(cls.seed))
        cls._run("git", "-C", str(cls.seed), "config", "user.name", "Fixture")
        cls._run("git", "-C", str(cls.seed), "config", "user.email", "fixture@example.test")
        (cls.seed / "src").mkdir()
        (cls.seed / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (cls.seed / "README.md").write_text("# Demo\n", encoding="utf-8")
        (cls.seed / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        cls._run("git", "-C", str(cls.seed), "add", ".")
        cls._run("git", "-C", str(cls.seed), "commit", "-m", "seed")
        cls._run("git", "-C", str(cls.seed), "remote", "add", "origin", str(cls.remote))
        cls._run("git", "-C", str(cls.seed), "push", "-u", "origin", "main")
        cls.registry.write_text(
            yaml.safe_dump(
                {
                    "repositories": [
                        {
                            "id": "demo",
                            "display_name": "Demo",
                            "root": str(cls.seed.resolve()),
                            "remote": str(cls.remote.resolve()),
                            "base_branch": "main",
                            "write_enabled": True,
                            "write_globs": ["src/**", "README.md", "AGENTS.md"],
                            "required_docs": ["README.md", "AGENTS.md"],
                            "allow_extensions": [".py", ".md"],
                            "checks": [
                                {
                                    "id": "verify",
                                    "argv": [
                                        os.environ.get("PYTHON", "python"),
                                        "-c",
                                        "from pathlib import Path; assert 'VALUE = 2' in Path('src/app.py').read_text()",
                                    ],
                                    "timeout_seconds": 30,
                                }
                            ],
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.runtime, ignore_errors=True)

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            os.environ,
            {
                "CHATCOPILOT_CODEBASE_REGISTRY": str(self.registry),
                "CHATCOPILOT_CODEBASE_CACHE_ROOT": str(self.cache),
                "CHATCOPILOT_BOT_ID": "test-bot",
            },
            clear=False,
        )
        self.env.start()
        reset_cache()
        self.service = RepositoryTaskService()

    def tearDown(self) -> None:
        reset_cache()
        self.env.stop()

    def test_prepare_patch_review_check_and_exact_overlay(self) -> None:
        state = self.service.prepare("demo", "Update the fixture", task_id="task-change-001")
        self.assertEqual(state.status, "prepared")
        patch = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # Demo
+Updated.
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1 +1,2 @@
 # Rules
+Updated.
"""
        state = self.service.apply_patch(state.change_id, patch)
        self.assertEqual(state.status, "changed")
        state = self.service.review(state.change_id, ok=True, summary="Diff is scoped and correct.")
        self.assertEqual(state.status, "reviewed")
        state, results = self.service.check(state.change_id)
        self.assertTrue(all(result["ok"] for result in results))
        state = self.service.publish(state.change_id)
        self.assertEqual(state.status, "published")
        self.assertFalse(state.commit_sha)
        remote_ref = self._run(
            "git", "--git-dir", str(self.remote), "show-ref", state.branch
            , check=False
        ).stdout
        self.assertNotIn(state.branch, remote_ref)
        self.assertEqual((self.seed / "src" / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")

    def test_required_docs_gate_blocks_publish(self) -> None:
        state = self.service.prepare("demo", "Missing docs", task_id="task-change-002")
        patch = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
        state = self.service.apply_patch(state.change_id, patch)
        with self.assertRaisesRegex(ValueError, "status 'changed'"):
            self.service.check(state.change_id)
        self.service.review(state.change_id, ok=True, summary="Code is correct but docs are absent.")
        with self.assertRaisesRegex(ValueError, "status 'reviewed'"):
            self.service.review(state.change_id, ok=True, summary="Duplicate review")
        _, results = self.service.check(state.change_id)
        self.assertTrue(all(result["ok"] for result in results))
        with self.assertRaisesRegex(ValueError, "required documentation"):
                self.service.publish(state.change_id)

    def test_z_conflict_stops_without_remote_delivery(self) -> None:
        state = self.service.prepare("demo", "Conflict test", task_id="task-change-003")
        patch = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # Demo
+Bot update.
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1 +1,2 @@
 # Rules
+Bot update.
"""
        state = self.service.apply_patch(state.change_id, patch)
        self.service.review(state.change_id, ok=True, summary="Pre-rebase diff is correct.")
        _, results = self.service.check(state.change_id)
        self.assertTrue(all(result["ok"] for result in results))

        (self.seed / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self._run("git", "-C", str(self.seed), "add", "src/app.py")
        self._run("git", "-C", str(self.seed), "commit", "-m", "advance main")
        self._run("git", "-C", str(self.seed), "push", "origin", "main")

        with self.assertRaisesRegex(RuntimeError, "target changed"):
            self.service.publish(state.change_id)
        remote_ref = self._run(
            "git", "--git-dir", str(self.remote), "show-ref", state.branch,
            check=False,
        )
        self.assertNotEqual(remote_ref.returncode, 0)

    @staticmethod
    def _run(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv), check=check, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )


if __name__ == "__main__":
    unittest.main()
