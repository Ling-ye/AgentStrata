from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import git_output, source_manifest
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.local_commit import LocalCommitter
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.models import HarnessError, RepairOptions
from chatcopilot.harness.workflow import run_task
from test_case_harness import FakeCoder, FakeEvaluator

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def repository(tmp_path_factory):
    root = tmp_path_factory.mktemp("quality-repository") / "source"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(ROOT), str(root)], check=True)
    if git_output(root, "branch", "--show-current") != "main":
        git_output(root, "branch", "main", "HEAD")
    git_output(root, "config", "user.name", "Lingye")
    git_output(root, "config", "user.email", "616202172@qq.com")
    return root


class ReviewedCoder(FakeCoder):
    def __init__(self, decision="approved", change=None):
        super().__init__()
        self.decision, self.change, self.reviews = decision, change, 0

    def review(self, worktree, evidence, options, output, check_cancel):
        check_cancel()
        self.reviews += 1
        assert evidence["reproduction"]["failed_cases"] == ["b"]
        assert evidence["verification"]["target"]["passed_cases"] == ["a", "b"]
        assert "harness_probe.py" in evidence["patch"]
        if self.change:
            self.change(worktree)
        return {
            "decision": self.decision,
            "problem": "" if self.decision == "approved" else "原问题仍可能存在",
            "reason": "controlled review evidence",
            "evidence_refs": ["source", "patch", "verification"],
        }


@pytest.fixture
def setup(repository, tmp_path):
    evaluator = FakeEvaluator()
    controller = HarnessController(repository, root=tmp_path / "private", evaluator=evaluator)
    task = controller.start(
        "eval-source",
        "sample-suite:b",
        "main",
        RepairOptions("test-model"),
        review_and_commit=True,
        launch=False,
    )
    publisher = LocalCommitter(ROOT)
    publisher.checks = Mock()
    return controller, task["task_id"], evaluator, publisher


def test_real_local_commit_preserves_operator_branch_and_creates_one_commit(setup):
    controller, task_id, evaluator, publisher = setup
    before = git_output(controller.repository, "rev-parse", "HEAD")
    coder = ReviewedCoder()
    result = run_task(controller.store, task_id, evaluator, coder, committer=publisher)
    assert result["status"] == "fixed", result.get("message")
    worktree = Path(result["worktree"])
    sha = result["local_commit"]["sha"]
    assert git_output(worktree, "rev-parse", "HEAD") == sha
    assert git_output(worktree, "rev-list", "--count", f"{before}..HEAD") == "1"
    assert git_output(worktree, "status", "--porcelain") == ""
    message = git_output(worktree, "log", "-1", "--format=%B")
    assert message.startswith("[AI Harness] 自动修复：") and "Generated-by: AI Harness" in message
    assert "eval-source" not in message
    assert message.splitlines()[0].endswith("：b")
    assert git_output(controller.repository, "rev-parse", "HEAD") == before
    assert coder.reviews == 1 and publisher.checks.call_count == 1
    public = controller.get(task_id)
    assert public["uncommitted"] is False and public["commit_in_main"] is False
    assert public["candidate_available"]


@pytest.mark.parametrize("decision, status", [("rejected", "failed"), ("inconclusive", "blocked")])
def test_negative_review_stops_without_commit_and_keeps_candidate(setup, decision, status):
    controller, task_id, evaluator, publisher = setup
    coder = ReviewedCoder(decision)
    result = run_task(controller.store, task_id, evaluator, coder, committer=publisher)
    assert result["status"] == status
    assert result["stage"] == "review" and "原问题" in result["message"]
    assert Path(result["worktree"], "src/chatcopilot/core/harness_probe.py").is_file()
    assert git_output(Path(result["worktree"]), "rev-parse", "HEAD") == result["base_commit"]
    assert "local_commit" not in result and coder.calls == 1 and coder.reviews == 1
    publisher.checks.assert_not_called()
    if status == "blocked":
        controller.store.update(task_id, status="queued")
        run_task(controller.store, task_id, evaluator, coder, committer=publisher)
        assert coder.reviews == 1


@pytest.mark.parametrize("change", ["product", "staging"])
def test_changes_after_validation_stop_publication(setup, change):
    controller, task_id, evaluator, publisher = setup

    def alter(worktree):
        if change == "product":
            (worktree / "src/chatcopilot/core/harness_probe.py").write_text("VALUE = 'outside'\n")
        else:
            git_output(worktree, "add", "--", "src/chatcopilot/core/harness_probe.py")

    result = run_task(
        controller.store, task_id, evaluator, ReviewedCoder(change=alter), committer=publisher
    )
    assert result["status"] == "blocked" and "local_commit" not in result
    assert result["error_code"] == ("workspace_changed" if change == "product" else "index_changed")
    assert git_output(Path(result["worktree"]), "rev-parse", "HEAD") == result["base_commit"]


def test_database_failure_after_git_commit_recovers_without_review_or_second_commit(
    setup, monkeypatch
):
    controller, task_id, evaluator, publisher = setup
    original = controller.store.update
    failed = False

    def update(task, **changes):
        nonlocal failed
        if "local_commit" in changes and not failed:
            failed = True
            raise OSError("controlled database outage")
        return original(task, **changes)

    monkeypatch.setattr(controller.store, "update", update)
    coder = ReviewedCoder()
    first = run_task(controller.store, task_id, evaluator, coder, committer=publisher)
    assert first["status"] == "interrupted"
    sha = git_output(Path(first["worktree"]), "rev-parse", "HEAD")
    assert sha != first["base_commit"] and "local_commit" not in first
    assert controller.get(task_id)["commit_state"] == "unconfirmed"
    controller.store.update(task_id, status="queued", elapsed_seconds=99999)
    second = run_task(controller.store, task_id, evaluator, coder, committer=publisher)
    assert second["status"] == "fixed" and second["local_commit"]["sha"] == sha
    assert coder.reviews == 1 and coder.calls == 1 and publisher.checks.call_count == 1
    assert (
        git_output(Path(first["worktree"]), "rev-list", "--count", f"{first['base_commit']}..HEAD")
        == "1"
    )


def test_enabling_review_changes_request_identity_but_old_requests_keep_behavior(
    repository, tmp_path
):
    controller = HarnessController(repository, root=tmp_path / "private", evaluator=FakeEvaluator())
    args = ("eval-source", "sample-suite:b", "main", RepairOptions("test-model"))
    old = controller.start(*args, request_id="stable", launch=False)
    new = controller.start(*args, review_and_commit=True, launch=False)
    assert old["task_id"] != new["task_id"] and not old["review_and_commit"]
    with pytest.raises(HarnessError, match="内容已变化"):
        controller.start(*args, request_id="stable", review_and_commit=True, launch=False)


def test_frozen_test_uses_final_repository_path_before_and_after_fix(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git_output(root, "init", "--quiet")
    (root / "src").mkdir()
    product = root / "src/probe.py"
    product.write_text("VALUE = 0\n")
    (root / "tests/unit").mkdir(parents=True)
    (root / "tests/unit/test_existing.py").write_text("def test_existing(): assert True\n")
    private = private_directory(tmp_path / "private")
    verifier = LocalVerifier(private)
    task = {
        "task_id": "repair-path",
        "review_and_commit": True,
        "source": {
            "kind": "robot_task",
            "target_id": "local-pytest",
            "repetitions": 1,
            "evidence": {},
        },
    }
    content = b"from probe import VALUE\ndef test_regression(): assert VALUE == 1\n"

    def prepare(_root, _evidence, _options, output, _check):
        draft = private_directory(output / "draft")
        (draft / "test_reproduction.py").write_bytes(content)
        (draft / "diagnosis.json").write_text(
            json.dumps(
                {
                    "reproducible": True,
                    "reason": "controlled fixture",
                    "expected_behavior": "VALUE equals one",
                }
            )
        )
        for file in draft.iterdir():
            file.chmod(0o600)
        return {}

    task["source"] = verifier.prepare(
        task, root, SimpleNamespace(prepare=prepare), RepairOptions("test-model"), lambda: None
    )
    source = task["source"]
    assert source["test_nodeid"].startswith("tests/unit/harness_regressions/test_")
    assert not (root / source["test_relative_path"]).exists()
    first = verifier.run(task, root, "before", ["reproduction"], lambda: None)
    product.write_text("VALUE = 1\n")
    second = verifier.run(task, root, "after", source["case_ids"], lambda: None)
    assert first["result"]["trials"][0]["outcome"] == "failed"
    assert len(second["result"]["trials"]) == 2
    assert {row["outcome"] for row in second["result"]["trials"]} == {"passed"}
    assert source["test_sha256"] == hashlib.sha256(content).hexdigest()
    assert not (root / source["test_relative_path"]).exists()
    # The same bytes are collected by ordinary repository pytest after adoption.
    destination = root / source["test_relative_path"]
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    task["source"] = {}
    result = verifier.regressions(task, root, lambda: None)
    assert len(result["passed_cases"]) == 1
    product.write_text("VALUE = 0\n")
    result = verifier.regressions(task, root, lambda: None)
    assert len(result["failed_cases"]) == 1
    assert source_manifest(root)[source["test_relative_path"]]["sha256"] == source["test_sha256"]


def test_robot_fix_and_identical_frozen_regression_share_one_commit(repository, tmp_path):
    from test_harness_sources import LocalFixture, robot_source

    content = b"import importlib.util\ndef test_behavior():\n    assert importlib.util.find_spec('chatcopilot.core.harness_probe') is not None\n"
    digest = hashlib.sha256(content).hexdigest()

    class Verifier(LocalFixture):
        def prepare(self, task, worktree, coder, options, check_cancel):
            folder = private_directory(tmp_path / "frozen")
            file = folder / "test_reproduction.py"
            file.write_bytes(content)
            file.chmod(0o600)
            return {
                **super().prepare(task, worktree, coder, options, check_cancel),
                "test_path": str(file),
                "test_sha256": digest,
                "test_relative_path": f"tests/unit/harness_regressions/test_{digest}.py",
            }

    coder = FakeCoder()

    def review(_worktree, evidence, *_args):
        assert evidence["regression"]["test"].encode() == content
        return {
            "decision": "approved",
            "problem": "",
            "reason": "controlled fixture",
            "evidence_refs": ["regression", "patch"],
        }

    coder.review = review
    controller = HarnessController(
        repository, root=tmp_path / "private", task_reader=lambda *_: robot_source()
    )
    task = controller.start_task(
        "sample", "run-example", RepairOptions("test-model"), review_and_commit=True, launch=False
    )
    publisher = LocalCommitter(ROOT)
    publisher.checks = Mock()
    result = run_task(
        controller.store,
        task["task_id"],
        Mock(),
        coder,
        local_verifier=Verifier(),
        committer=publisher,
    )
    assert result["status"] == "fixed", result.get("message")
    root = Path(result["worktree"])
    assert (root / result["regression"]["path"]).read_bytes() == content
    changed = git_output(
        root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
    ).splitlines()
    assert set(changed) == {"src/chatcopilot/core/harness_probe.py", result["regression"]["path"]}
    assert git_output(root, "status", "--porcelain") == ""
    assert git_output(root, "log", "-1", "--format=%s").endswith("：test_behavior")


def test_actual_public_boundary_blocks_private_candidate_before_commit(setup):
    controller, task_id, evaluator, _publisher = setup
    coder = ReviewedCoder()
    coder.candidates = ["fixed http://" + "10" + ".0.0.9/private"]
    objects = Path(
        git_output(
            controller.repository, "rev-parse", "--path-format=absolute", "--git-path", "objects"
        )
    )
    before_objects = {
        str(file.relative_to(objects)) for file in objects.rglob("*") if file.is_file()
    }
    result = run_task(controller.store, task_id, evaluator, coder, committer=LocalCommitter(ROOT))
    assert result["status"] == "blocked", result.get("message")
    assert result["error_code"] == "commit_check_failed"
    assert before_objects == {
        str(file.relative_to(objects)) for file in objects.rglob("*") if file.is_file()
    }
    assert "公开信息" in result["message"] and "local_commit" not in result
    assert git_output(Path(result["worktree"]), "rev-parse", "HEAD") == result["base_commit"]
    assert git_output(Path(result["worktree"]), "diff", "--cached", "--name-only") == ""


def test_review_adapter_uses_new_read_only_session_and_keeps_credentials_private(
    tmp_path, monkeypatch
):
    import contextlib
    from chatcopilot.harness import codex_adapter

    root = tmp_path / "source"
    root.mkdir()
    (root / "src/chatcopilot/core").mkdir(parents=True)
    output = tmp_path / "review"
    adapter = codex_adapter.CodexCoder()
    monkeypatch.setattr(adapter, "preflight", lambda: (Path("/usr/bin/true"), tmp_path / "auth"))
    monkeypatch.setattr(
        codex_adapter, "credential_lease", lambda *_args, **_kwargs: contextlib.nullcontext()
    )
    seen = {}

    def permissions(scope, **kwargs):
        seen["scope"] = scope
        seen["permissions"] = kwargs
        return ()

    monkeypatch.setattr(codex_adapter, "permission_config", permissions)
    monkeypatch.setattr(
        codex_adapter, "build_codex_command", lambda *_args, **_kwargs: ["codex", "exec"]
    )
    monkeypatch.setattr(codex_adapter, "build_codex_subprocess_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        codex_adapter, "sandbox_command", lambda command, **_kwargs: ["bwrap", "--", *command]
    )
    decision = {
        "decision": "approved",
        "problem": "",
        "reason": "review fixture",
        "evidence_refs": ["patch"],
    }

    def process(command, **kwargs):
        seen["command"] = command
        kwargs["on_stdout_line"](
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(decision)},
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_adapter, "run_codex_process", process)
    result = adapter.review(root, {"source": {}}, RepairOptions("test-model"), output, lambda: None)
    assert result["decision"] == "approved"
    assert seen["scope"].writable_roots == () and not seen["scope"].native_write
    assert seen["permissions"]["private_paths"] == (str(output / "codex-home"),)
    assert seen["permissions"]["network_access"] is False
    assert "resume" not in seen["command"]


def test_review_exception_persists_inconclusive_result_and_does_not_retry(setup):
    controller, task_id, evaluator, publisher = setup
    coder = ReviewedCoder()
    coder.review = Mock(side_effect=RuntimeError("controlled review failure"))
    first = run_task(controller.store, task_id, evaluator, coder, committer=publisher)
    assert first["status"] == "blocked" and first["error_code"] == "review_inconclusive"
    controller.store.update(task_id, status="queued")
    second = run_task(controller.store, task_id, evaluator, coder, committer=publisher)
    assert second["status"] == "blocked" and coder.review.call_count == 1
    assert "local_commit" not in second
