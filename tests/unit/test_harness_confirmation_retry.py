"""Explicit environment recovery keeps one candidate and immutable old evidence."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.models import HarnessError, VerificationCheck, VerificationResult
from chatcopilot.harness.repair_runtime import run_task
from test_harness_repair_v2 import Coder, Verifier, proposal, repair as repair_fixture


@pytest.fixture
def repair(tmp_path):
    return repair_fixture.__wrapped__(tmp_path)


class EnvironmentConfirmation(Verifier):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.regression_calls = 0
        self.fail_confirmation = True

    def prepare(self, *args):
        source, plan = super().prepare(*args)
        return source, replace(plan, real_agent=True, repetitions=3)

    def run(self, task, candidate, ident, checks, cancel):
        self.ids.append(ident)
        result = super().run(task, candidate, ident, checks, cancel)
        failed = "confirm" in ident and self.fail_confirmation
        return VerificationResult(ident, candidate.digest, tuple(
            VerificationCheck("target", n, "error" if failed else result.checks[0].outcome,
                              "environment" if failed else result.checks[0].failure_kind)
            for n in range(1, 4)))

    def regressions(self, *args):
        self.regression_calls += 1
        return super().regressions(*args)


def controller_for(store, repo):
    workers = SimpleNamespace(observe=lambda task: WorkerState.INACTIVE,
                             launch=lambda task: SimpleNamespace(state="started", message="", code=None))
    return HarnessController(repo, root=store.root, worker_control=workers,
                             evaluator=SimpleNamespace(get=lambda ident: {"status": "completed"}))


def test_resume_retries_whole_confirmation_without_recoding_or_repeating_completed_checks(repair):
    store, ident, repo = repair
    verifier, coder = EnvironmentConfirmation(), Coder()
    first = run_task(store, ident, verifier, coder)
    assert first["error_code"] == "verification_environment" and not first.get("accepted_candidate")
    old_receipts = dict(first["operations"])
    old_confirmation = first["evaluations"]["confirm-1"]
    elapsed = first["elapsed_seconds"]
    controller = controller_for(store, repo)
    controller.resume(ident)
    verifier.fail_confirmation = False
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "fixed", result.get("message")
    assert coder.calls == 1 and len(store.attempts(ident)) == 1
    assert len(verifier.ids) == 4 and verifier.ids[-1].endswith("confirm-1-retry-1")
    assert verifier.regression_calls == 2  # One baseline and one candidate, before the retry.
    assert result["elapsed_seconds"] >= elapsed
    assert result["evaluations"]["confirm-1"] == old_confirmation
    assert all(result["operations"][key] == value for key, value in old_receipts.items())
    assert result["accepted_candidate"]["candidate_digest"] == first["working_digest"]


@pytest.mark.parametrize("drift", ["source", "plan", "goal", "snapshot", "baseline", "unknown_operation"])
def test_confirmation_retry_rejects_drift_without_allocating_a_retry(repair, drift):
    store, ident, repo = repair
    first = run_task(store, ident, EnvironmentConfirmation(), Coder())
    if drift == "source":
        store.update(ident, source={**first["source"], "test_sha256": "changed"})
    elif drift == "plan":
        store.update(ident, verification_plan={**first["verification_plan"], "checks": ["different"]})
    elif drift == "goal":
        store.update(ident, acceptance={**first["acceptance"], "sha256": "changed"})
    elif drift == "snapshot":
        attempt = store.attempts(ident)[0]
        (Path(attempt["snapshot_path"]) / "src/chatcopilot/core/probe.py").write_text("VALUE = 2\n")
    elif drift == "baseline":
        (store.root / "jobs" / ident / "snapshots/baseline/src/chatcopilot/core/probe.py").write_text("VALUE = 2\n")
    else:
        operations = dict(first["operations"])
        operations["verification:confirm-1"] = {**operations["verification:confirm-1"], "state": "unknown"}
        store.update(ident, operations=operations)
    with pytest.raises(HarnessError, match="独立确认重验"):
        controller_for(store, repo).resume(ident)
    assert not store.get(ident).get("verification_retries")


def test_crash_after_graph_fork_does_not_allocate_another_confirmation_retry(repair, monkeypatch):
    from langgraph.pregel import Pregel

    store, ident, repo = repair
    verifier, coder = EnvironmentConfirmation(), Coder()
    run_task(store, ident, verifier, coder)
    original = Pregel.update_state

    class ProcessStopped(BaseException):
        pass

    def stop_after_fork(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise ProcessStopped()

    with monkeypatch.context() as patch:
        patch.setattr(Pregel, "update_state", stop_after_fork)
        with pytest.raises(ProcessStopped):
            controller_for(store, repo).resume(ident)
    controller_for(store, repo).resume(ident)
    verifier.fail_confirmation = False
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "fixed", result.get("message")
    assert len(result["verification_retries"]) == 1 and coder.calls == 1


def test_environment_retry_does_not_hide_a_product_failure_in_the_same_confirmation(repair):
    store, ident, repo = repair

    class MixedFailure(EnvironmentConfirmation):
        def run(self, task, candidate, run_id, checks, cancel):
            result = super().run(task, candidate, run_id, checks, cancel)
            if "confirm" in run_id:
                return replace(result, checks=(VerificationCheck("target", 1, "failed", "product"),
                                               *result.checks[1:]))
            return result

    first = run_task(store, ident, MixedFailure(), Coder())
    assert first["error_code"] == "verification_environment"
    with pytest.raises(HarnessError, match="独立确认重验"):
        controller_for(store, repo).resume(ident)
    assert not store.get(ident).get("verification_retries")


def test_each_explicit_retry_keeps_the_latest_failed_confirmation_and_one_candidate(repair):
    store, ident, repo = repair
    verifier, coder = EnvironmentConfirmation(), Coder()
    run_task(store, ident, verifier, coder)
    controller_for(store, repo).resume(ident)
    second = run_task(store, ident, verifier, coder)
    assert store.attempts(ident)[0]["confirmation"]["evaluation_id"].endswith("confirm-1-retry-1")
    controller_for(store, repo).resume(ident)
    verifier.fail_confirmation = False
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "fixed", result.get("message")
    assert len(result["verification_retries"]) == 2 and coder.calls == 1
    assert set(second["evaluations"]).issubset(result["evaluations"])
    assert len(verifier.ids) == 5 and verifier.ids[-1].endswith("confirm-1-retry-2")


def test_exhausted_task_requires_an_explicit_budget_increase_before_resume(repair):
    store, ident, repo = repair
    store.update(ident, options={**store.get(ident)["options"], "max_attempts": 1})
    result = run_task(store, ident, Verifier(), Coder(values=(0,)))
    assert result["status"] == "failed" and result["error_code"] == "attempts_exhausted"
    controller = controller_for(store, repo)
    with pytest.raises(HarnessError, match="不能恢复"):
        controller.resume(ident)
    assert store.claim_resume(ident)[1] is False
    before = store.get(ident)["elapsed_seconds"]
    store.update(ident, options={**store.get(ident)["options"], "max_attempts": 2})
    assert controller.resume(ident)["status"] == "queued"
    result = run_task(store, ident, Verifier(), Coder())
    assert result["status"] == "fixed", result.get("message")
    assert result["elapsed_seconds"] >= before and len(store.attempts(ident)) == 2


def test_regression_budget_stop_resumes_verified_candidate_without_another_code_round(repair):
    store, ident, repo = repair

    class Limited(Verifier):
        stopped = False

        def regressions(self, *args):
            if not self.stopped:
                self.stopped = True
                raise HarnessError("budget_exhausted", "fixture regression allocation")
            return super().regressions(*args)

    verifier, coder = Limited(), Coder()
    first = run_task(store, ident, verifier, coder)
    assert first["status"] == "blocked" and first["error_code"] == "budget_exhausted"
    controller_for(store, repo).resume(ident)
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "fixed", result.get("message")
    assert coder.calls == 1 and len(store.attempts(ident)) == 1
    assert result["verification_retries"][0]["stage"] == "regressions"


def _reviewed_pytest_gap(repair):
    import hashlib
    from chatcopilot.core.private_sqlite import json_text

    store, ident, repo = repair
    content = b"def test_frozen_definition():\n    assert True\n"
    sha = hashlib.sha256(content).hexdigest()
    path = store.root / "jobs" / ident / "reproducer/frozen" / sha / "test_reproduction.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    store.update(ident, source={**store.get(ident)["source"], "test_sha256": sha, "test_path": str(path),
                               "test_relative_path": f"tests/unit/harness_regressions/test_{sha}.py"})
    gap = {"requirement": "expected_behavior", "code": "fixture_missing", "message": "independent pytest draft unavailable"}
    verifier = EnvironmentConfirmation()
    verifier.fail_confirmation = False
    coder = Coder(proposed=proposal(gaps=[gap]))
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "needs_review" and store.attempts(ident)[-1]["review"]["decision"] == "approved", result.get("message")
    return store, ident, repo, verifier, coder, path, hashlib.sha256(json_text(gap).encode()).hexdigest()


def test_verified_pytest_material_closes_only_selected_claim_without_changing_grades(repair):
    from chatcopilot.harness.artifact_repository import ArtifactRepository

    store, ident, repo, verifier, coder, path, gap_digest = _reviewed_pytest_gap(repair)
    before = store.attempts(ident)[-1]
    controller_for(store, repo).reconcile_pytest_gap(ident, gap_digest)
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "fixed", result.get("message")
    after = store.attempts(ident)[-1]
    assert after["gaps"] == [] and after["submission"]["gaps"] == before["submission"]["gaps"]
    assert all(after[field] == before[field] for field in ("verification", "confirmation", "review"))
    assert coder.calls == 1 and len(verifier.ids) == 3
    proof = ArtifactRepository(store.root / "jobs" / ident).read(after["material_resolutions"][0])
    assert proof["gap_digest"] == gap_digest and proof["test_sha256"] == path.parent.name


@pytest.mark.parametrize("drift", ["test_file", "goal", "review", "binding"])
def test_pytest_material_resolution_rejects_missing_or_changed_frozen_evidence(repair, drift):
    store, ident, repo, _, _, path, gap_digest = _reviewed_pytest_gap(repair)
    task = store.get(ident)
    if drift == "test_file":
        path.write_text("changed")
    elif drift == "goal":
        store.update(ident, acceptance={**task["acceptance"], "sha256": "changed"})
    elif drift == "binding":
        store.update(ident, source={**task["source"], "conditions": {"changed": True}})
    else:
        attempt = store.attempts(ident)[-1]
        attempt["review"] = {**attempt["review"], "decision": "inconclusive"}
        store.save_attempt(ident, attempt["number"], attempt)
    with pytest.raises(HarnessError, match="pytest"):
        controller_for(store, repo).reconcile_pytest_gap(ident, gap_digest)
    assert store.get(ident)["status"] == "needs_review"
    assert not store.attempts(ident)[-1].get("material_resolutions")
