from __future__ import annotations

import copy
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chatcopilot.core.source_snapshot import git_output, manifest_digest, source_manifest
from chatcopilot.evals.application.result_store import EvaluationResultStore
from chatcopilot.evals.application import EvaluationApplication
from chatcopilot.evals.code_source import prepare_code_source
from chatcopilot.evals.conditions import verify_conditions
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.models import HarnessError, RepairOptions, passed_cases
from chatcopilot.harness.workflow import run_task

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def repository(tmp_path_factory):
    root = tmp_path_factory.mktemp("harness-repository") / "source"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(ROOT), str(root)], check=True)
    yield root


def source_record():
    return {
        "evaluation_id": "eval-source",
        "bot_id": "sample",
        "suite_id": "sample-suite",
        "case_ref": "sample-suite:b",
        "case_id": "b",
        "target_id": "main",
        "case_ids": ["a", "b", "c"],
        "repetitions": 2,
        "passed_cases": ["a"],
        "trials": [],
        "failure_signature": [{"error_code": "wrong_result"}],
        "conditions": {
            "cases": {"a": "sha-a", "b": "sha-b", "c": "sha-c"},
            "targets": {"main": {"model": "test-model"}},
            "grading": {},
            "environment": "test",
        },
    }


class FakeEvaluator:
    def __init__(self, *, current_passes=False, interrupt_verification=False):
        self.calls = []
        self.current_passes = current_passes
        self.interrupt_verification = interrupt_verification

    def source(self, evaluation_id, case_ref, target_id):
        return source_record()

    def run(self, task, worktree, evaluation_id, case_ids, check_cancel):
        check_cancel()
        self.calls.append(evaluation_id)
        marker = worktree / "src/chatcopilot/core/harness_probe.py"
        content = marker.read_text() if marker.exists() else ""
        if "verify" in evaluation_id and self.interrupt_verification:
            self.interrupt_verification = False
            raise HarnessError("evaluation_unavailable", "temporarily unavailable")
        rows = []
        for case in case_ids:
            passed = (case == "a" and "regression" not in content) or (
                case == "b" and (self.current_passes or "fixed" in content)
            )
            for attempt in range(1, 3):
                rows.append(
                    {
                        "case_id": case,
                        "target_id": "main",
                        "attempt": attempt,
                        "outcome": "passed" if passed else "failed",
                    }
                )
        return {"result": {"trials": rows}}

    def cancel(self, evaluation_id):
        pass


class FakeCoder:
    def __init__(self, candidates=("fixed",)):
        self.candidates = list(candidates)
        self.calls = 0

    def run(self, worktree, evidence, options, output, check_cancel):
        check_cancel()
        text = self.candidates[min(self.calls, len(self.candidates) - 1)]
        self.calls += 1
        (worktree / "src/chatcopilot/core/harness_probe.py").write_text(f"VALUE = {text!r}\n")
        return {"summary": text}


def task_fixture(repository, tmp_path, evaluator, *, attempts=3):
    controller = HarnessController(repository, root=tmp_path / "private", evaluator=evaluator)
    result = controller.start(
        "eval-source",
        "sample-suite:b",
        "main",
        RepairOptions("test-model", max_attempts=attempts),
        request_id="test-request",
        launch=False,
    )
    return controller, result["task_id"]


def test_current_pass_ends_without_coder(repository, tmp_path):
    evaluator, coder = FakeEvaluator(current_passes=True), FakeCoder()
    controller, task_id = task_fixture(repository, tmp_path, evaluator)
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "not_reproduced"
    assert coder.calls == 0 and len(evaluator.calls) == 1


def test_fix_preserves_original_failure_and_allows_other_existing_failures(repository, tmp_path):
    evaluator, coder = FakeEvaluator(), FakeCoder()
    controller, task_id = task_fixture(repository, tmp_path, evaluator)
    original = copy.deepcopy(controller.store.get(task_id)["source"])
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "fixed"
    assert result["source"] == original
    assert coder.calls == 1
    attempt = controller.store.attempts(task_id)[0]
    assert attempt["verification"]["failed_cases"] == ["c"]
    assert attempt["verification"]["passed_cases"] == ["a", "b"]
    assert controller.get(task_id)["candidate_available"] is True
    assert b"harness_probe.py" in controller.patch(task_id, 1)
    assert git_output(Path(result["worktree"]), "rev-parse", "HEAD") == result["base_commit"]


def test_regression_rejects_candidate_and_next_attempt_starts_from_base(repository, tmp_path):
    evaluator, coder = FakeEvaluator(), FakeCoder(("fixed regression", "fixed"))
    controller, task_id = task_fixture(repository, tmp_path, evaluator)
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "fixed"
    attempts = controller.store.attempts(task_id)
    assert [item["status"] for item in attempts] == ["rejected", "accepted"]
    assert attempts[0]["regressions"] == ["a"]
    assert b"regression" in controller.patch(task_id, 1)


def test_verification_resume_reuses_candidate_without_replaying_coder(repository, tmp_path):
    evaluator, coder = FakeEvaluator(interrupt_verification=True), FakeCoder()
    controller, task_id = task_fixture(repository, tmp_path, evaluator)
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "blocked"
    pending = result["current_evaluation_id"]
    controller.store.update(task_id, status="queued")
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "fixed" and coder.calls == 1
    assert evaluator.calls.count(pending) == 2


def test_no_false_success_after_attempt_limit(repository, tmp_path):
    evaluator, coder = FakeEvaluator(), FakeCoder(("still broken",))
    controller, task_id = task_fixture(repository, tmp_path, evaluator, attempts=1)
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "failed" and "verified_digest" not in result
    assert not (Path(result["worktree"]) / "src/chatcopilot/core/harness_probe.py").exists()


def test_idempotent_create_and_request_drift(repository, tmp_path):
    controller, task_id = task_fixture(repository, tmp_path, FakeEvaluator())
    options = RepairOptions("test-model")
    same = controller.start(
        "eval-source", "sample-suite:b", "main", options, request_id="another-request", launch=False
    )
    assert same["task_id"] == task_id
    with pytest.raises(HarnessError, match="内容已变化"):
        controller.start(
            "eval-source",
            "sample-suite:b",
            "main",
            RepairOptions("other-model"),
            request_id="test-request",
            launch=False,
        )


def test_fixed_candidate_reuse_and_invalidated_workspace(repository, tmp_path):
    evaluator, coder = FakeEvaluator(), FakeCoder()
    controller, task_id = task_fixture(repository, tmp_path, evaluator)
    result = run_task(controller.store, task_id, evaluator, coder)
    reused = controller.start(
        "eval-source",
        "sample-suite:b",
        "main",
        RepairOptions("test-model"),
        request_id="later",
        launch=False,
    )
    assert reused["task_id"] == task_id and reused["reused"]
    (Path(result["worktree"]) / "src/chatcopilot/core/harness_probe.py").write_text(
        "VALUE = 'changed'\n"
    )
    assert controller.get(task_id)["candidate_available"] is False
    fresh = controller.start(
        "eval-source",
        "sample-suite:b",
        "main",
        RepairOptions("test-model"),
        request_id="fresh",
        launch=False,
    )
    assert fresh["task_id"] != task_id


def test_cancel_before_execution(repository, tmp_path):
    evaluator, coder = FakeEvaluator(), FakeCoder()
    controller, task_id = task_fixture(repository, tmp_path, evaluator)
    controller.cancel(task_id)
    controller.store.update(task_id, status="cancel_requested")
    result = run_task(controller.store, task_id, evaluator, coder)
    assert result["status"] == "cancelled"
    assert not evaluator.calls and not coder.calls


@pytest.mark.parametrize(
    "rows", [[], [{"case_id": "a", "target_id": "main", "attempt": 1, "outcome": "passed"}] * 2]
)
def test_missing_or_duplicate_trials_never_pass(rows):
    with pytest.raises(HarnessError):
        passed_cases({"trials": rows}, "main", ["a"], 2)


def test_result_database_is_explicit_and_idempotent(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals")
    request = {"evaluation_id": "eval-new", "case_ids": ["b"]}
    result = {
        "evaluation_id": "eval-new",
        "trials": [
            {
                "evaluation_id": "eval-new",
                "trial_id": "trial-b",
                "case_id": "b",
                "target_id": "main",
                "attempt": 1,
                "outcome": "failed",
                "events": [{"kind": "tool", "result": "error"}],
            }
        ],
    }
    store.register(request)
    for _ in range(2):
        store.synchronize("eval-new", result=result, state={"status": "completed"})
    assert store.get("eval-new")["result"] == result
    assert store.get("eval-old") is None
    assert store.pending() == []
    altered = copy.deepcopy(result)
    altered["trials"][0]["outcome"] = "passed"
    with pytest.raises(ValueError, match="changed"):
        store.synchronize("eval-new", result=altered, state={"status": "completed"})
    assert store.get("eval-new")["result"] == result
    store.delete("eval-new")
    assert store.get("eval-new") is None


def test_candidate_loads_changed_product_but_trusted_evaluator(repository, tmp_path):
    candidate = tmp_path / "candidate"
    git_output(repository, "worktree", "add", "--detach", str(candidate), "HEAD")
    probe = candidate / "src/chatcopilot/core/harness_probe.py"
    probe.write_text("VALUE = 'candidate'\n")
    verifier = "src/chatcopilot/evals/capability_verifiers.py"
    (candidate / verifier).write_text("raise RuntimeError('untrusted evaluator')\n")
    snapshot = tmp_path / "snapshot"
    receipt = prepare_code_source(
        repository,
        {"path": str(candidate), "sha256": manifest_digest(source_manifest(candidate))},
        snapshot,
    )
    assert (snapshot / verifier).read_bytes() == (repository / verifier).read_bytes()
    result = subprocess.run(
        [sys.executable, "-c", "from chatcopilot.core.harness_probe import VALUE; print(VALUE)"],
        cwd=snapshot,
        env={"PYTHONPATH": str(snapshot / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "candidate"
    assert receipt["sha256"] == manifest_digest(source_manifest(candidate))


def test_changed_scoring_conditions_fail_before_execution():
    expected = source_record()["conditions"]
    actual = copy.deepcopy(expected)
    actual["grading"] = {"threshold": 0}
    with pytest.raises(ValueError, match="grading"):
        verify_conditions(expected, actual)


def test_exception_records_redact_inherited_sensitive_environment(monkeypatch):
    from chatcopilot.harness.models import safe_error

    secret = "fixture" + "-private-value"
    monkeypatch.setenv("HTTP_PROXY", secret)
    assert secret not in safe_error(RuntimeError("failed command " + secret))


def test_private_database_rejects_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("preserve")
    folder = tmp_path / "evals"
    folder.mkdir(mode=0o700)
    (folder / "results.sqlite3").symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        EvaluationResultStore(folder)
    assert target.read_text() == "preserve"


def test_terminal_completion_wins_over_stale_worker_probe(repository, tmp_path, monkeypatch):
    controller, task_id = task_fixture(repository, tmp_path, FakeEvaluator())
    controller.store.update(task_id, status="running", dispatch_state="scheduled")

    def completed(_unit):
        controller.store.update(task_id, status="fixed")
        return False

    monkeypatch.setattr(controller, "_unit_active", completed)
    assert controller.get(task_id)["status"] == "fixed"


def test_resume_claim_is_taken_once(repository, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    controller, task_id = task_fixture(repository, tmp_path, FakeEvaluator())
    controller.store.update(task_id, status="interrupted")
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: controller.store.claim_resume(task_id)[1], range(2)))
    assert sorted(outcomes) == [False, True]


def test_operator_config_does_not_execute_shell(tmp_path, monkeypatch):
    from chatcopilot.harness.config import configuration

    config = tmp_path / "harness.env"
    config.write_text(
        "CHATCOPILOT_CODEX_BIN='$HOME/bin/codex'\nCHATCOPILOT_HARNESS_MODEL=$(invalid-command)\n"
    )
    config.chmod(0o600)
    monkeypatch.setenv("CHATCOPILOT_HARNESS_ENV", str(config))
    monkeypatch.delenv("CHATCOPILOT_CODEX_BIN", raising=False)
    monkeypatch.delenv("CHATCOPILOT_HARNESS_MODEL", raising=False)
    values = configuration()
    assert values["CHATCOPILOT_CODEX_BIN"] == str(Path.home() / "bin/codex")
    assert values["CHATCOPILOT_HARNESS_MODEL"] == "$(invalid-command)"
    monkeypatch.setenv("CHATCOPILOT_HARNESS_MODEL", "explicit-model")
    assert configuration()["CHATCOPILOT_HARNESS_MODEL"] == "explicit-model"


def test_repair_markers_are_not_limited_to_recent_global_history(repository, tmp_path, monkeypatch):
    earlier = time.time() - 3600
    with monkeypatch.context() as patch:
        patch.setattr("chatcopilot.harness.store.time.time", lambda: earlier)
        controller, task_id = task_fixture(repository, tmp_path, FakeEvaluator())
    original = controller.store.get(task_id)
    for number in range(101):
        controller.store.create(
            {
                **original,
                "task_id": f"other-{number}",
                "request_key": f"other-{number}",
                "active_key": f"other-{number}",
                "context_key": "other-context",
                "source": {**original["source"], "evaluation_id": f"eval-other-{number}"},
            }
        )
    assert all(task["task_id"] != task_id for task in controller.store.history())
    related = controller.store.related("eval-source", {original["context_key"]})
    assert [task["task_id"] for task in related] == [task_id]


@pytest.mark.parametrize("dry_run", [True, False])
def test_managed_candidate_bootstrap_and_database(tmp_path, dry_run):
    application = EvaluationApplication(tmp_path / "evaluations", repository_root=ROOT)
    record = application.start(
        bot_id="lingye-copilot-qq",
        evaluation_id="eval-source-smoke",
        request={
            "kind": "suite",
            "suite_id": "agentstrata-qq-message-flow-v1",
            "preset": "custom",
            "case_ids": ["qq-synthetic-roundtrip"],
            "dry_run": dry_run,
        },
        code_source={"path": str(ROOT), "sha256": manifest_digest(source_manifest(ROOT))},
    )
    deadline = time.monotonic() + 40
    try:
        while record["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            record = application.get(record["evaluation_id"])
        assert record["status"] == "completed", (
            record,
            (application.root / record["evaluation_id"] / "run.log").read_text(),
        )
        assert record["result_storage"] == "database"
        assert len(record["result"]["trials"]) == 1
        assert record["result"]["trials"][0]["outcome"] == ("skipped" if dry_run else "passed")
        assert record["code_source"]["sha256"]
        stored = application.result_store.get(record["evaluation_id"])
        assert stored["result"] == {key: value for key, value in record["result"].items() if key != "execution_observation"}
        if not dry_run:
            assert stored["observation"] == record["result"]["execution_observation"]
    finally:
        if record["status"] in {"queued", "running"}:
            application.cancel(record["evaluation_id"])
