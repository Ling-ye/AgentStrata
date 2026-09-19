"""Repair-v2 host contracts: bounded work, immutable evidence and partial outcomes."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess

from tests.harness_delivery_fixture import RoleFixture
import pytest

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.models import RepairOptions, VerificationPlan, VerificationResult, VerificationCheck, HarnessError
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.repair_runtime import run_task
from chatcopilot.harness.preparation import acceptance
from chatcopilot.harness.repair_types import ActionProgress


def proposal(**changes):
    return {"decision": "candidate", "summary": "fix observed value", "verification_kind": "pytest",
            "goal_capabilities": [], "coverage": [{"requirement": "expected_behavior", "checks": ["target"]}],
            "gaps": [], "notes": [], **changes}


@pytest.fixture
def repair(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, value in {"src/chatcopilot/core/probe.py": "VALUE = 0\n", ".gitignore": "__pycache__/\n"}.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture")
    store = HarnessStore(tmp_path / "private")
    ident = "repair-" + "a" * 32
    source = {"kind": "robot_task", "original_input": "Return one", "bot_id": "fixture"}
    store.create({"task_id": ident, "pipeline_version": 9, "request_key": "request", "request_digest": "req",
                  "match_key": "match", "context_key": "context", "active_key": "active", "source": source,
                  "repository": str(repo), "base_commit": git("rev-parse", "HEAD"), "options": asdict(RepairOptions("fixture"))})
    from chatcopilot.harness.artifact_repository import ArtifactRepository
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    store.update(ident, principles=asdict(artifacts.principles(Path(__file__).resolve().parents[2])))
    return store, ident, repo


class Verifier:
    def __init__(self, *, covered=True):
        self.paths = []
        self.covered = covered

    def capabilities(self):
        return {"fixtures": ["workspace"]}

    def prepare(self, task, candidate, output, proposed, cancel):
        return task["source"], VerificationPlan(
            ("target",), ("target",), (), 1, coverage={"expected_behavior": ["target"] if self.covered else []})

    def run(self, task, candidate, ident, checks, cancel):
        self.paths.append(candidate.path)
        passed = "VALUE = 1" in (candidate.path / "src/chatcopilot/core/probe.py").read_text()
        return VerificationResult(ident, candidate.digest,
                                  (VerificationCheck("target", 1, "passed" if passed else "failed", "" if passed else "product"),))

    def regressions(self, *args):
        return {"case_ids": [], "passed_cases": [], "failed_cases": []}




class Coder(RoleFixture):
    def __init__(self, values=(1,), proposed=None):
        self.values, self.proposed, self.calls = values, proposed, 0

    def run(self, root, evidence, options, output, cancel):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        (root / "src/chatcopilot/core/probe.py").write_text(f"VALUE = {value}\n")
        return {"submission": self.proposed or proposal()}

    def review(self, *args):
        return {"decision": "approved", "problem": "", "reason": "real product checks match goal",
                "evidence_refs": ["source", "verification", "patch"]}


def test_one_round_preserves_operator_tree_and_independent_snapshots(repair):
    store, ident, repo = repair
    verifier, coder = Verifier(), Coder()
    result = run_task(store, ident, verifier, coder)
    assert result["status"] == "fixed", result.get("message")
    assert coder.calls == 1
    assert (repo / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 0\n"
    assert verifier.paths[0] != verifier.paths[1] != Path(result["worktree"])
    assert "VALUE = 0" in (verifier.paths[0] / "src/chatcopilot/core/probe.py").read_text()
    assert "VALUE = 1" in (verifier.paths[1] / "src/chatcopilot/core/probe.py").read_text()
    assert "flow_steps" not in result and store.flow_steps(ident)
    assert not result.get("preparation_revisions")


def test_partial_coverage_retains_candidate_without_delivery(repair):
    from chatcopilot.harness.delivery_candidate import accepted
    store, ident, _ = repair
    result = run_task(store, ident, Verifier(covered=False), Coder())
    assert result["status"] == "needs_review", result.get("message")
    assert result["verification_gaps"] and result["candidate_checkpoint"]["changed_files"]
    assert not accepted(store, result)


def test_product_feedback_can_improve_second_candidate(repair):
    store, ident, _ = repair
    coder = Coder((2, 1))
    result = run_task(store, ident, Verifier(), coder)
    assert result["status"] == "fixed", result.get("message")
    assert coder.calls == 2 and len(store.attempts(ident)) == 2


def test_26_paraphrased_blockers_do_not_create_preparation_loop(repair):
    store, ident, _ = repair
    capture = json.loads((Path(__file__).parents[1] / "fixtures/harness/legacy_preparation_26.json").read_text())
    assert len(capture["revisions"]) == 26 and capture["coding_attempts"] == 0
    assert len({row["message_sha256"] for row in capture["revisions"]}) == 26
    assert capture["elapsed_seconds"] == pytest.approx(7200.9, abs=.1)
    revisions = iter(capture["revisions"])
    class Replay(Coder):
        def run(self, *args):
            row = next(revisions)
            self.proposed = proposal(decision="blocked", summary="Unavailable dependency: " + row["message_sha256"],
                gaps=[{"requirement": "expected_behavior", "code": "fixture_missing", "message": row["message_sha256"]}])
            return super().run(*args)
    coder = Replay()
    result = run_task(store, ident, Verifier(), coder)
    assert result["error_code"] == "fixture_missing" and coder.calls == 1
    assert len(store.attempts(ident)) == 1 and not result.get("preparation_revisions")


def test_historical_definition_paraphrases_exhaust_no_progress_not_versions(repair):
    store, ident, _ = repair
    capture = json.loads((Path(__file__).parents[1] / "fixtures/harness/legacy_preparation_26.json").read_text())
    rows = iter(row for row in capture["revisions"] if row["legacy_code"] == "test_definition")
    class InvalidDefinitions(Verifier):
        def prepare(self, *args):
            row = next(rows)
            raise HarnessError(row["legacy_code"], row["message_sha256"])
    coder = Coder((2, 3, 4))
    result = run_task(store, ident, InvalidDefinitions(), coder)
    assert result["stop_reason"] == "no_progress" and coder.calls == 1
    assert len(store.attempts(ident)) == 2


def test_two_rounds_without_evidence_stop_before_third(repair):
    store, ident, _ = repair
    coder = Coder((2, 3, 4))
    result = run_task(store, ident, Verifier(), coder)
    assert result["error_code"] == "no_progress", result
    assert coder.calls == 2
    feedback = store.attempts(ident)[-1]["feedback"]
    assert set(feedback) == {"stage", "code", "message", "requirements", "passed_requirements", "signature", "brief_ref"}
    brief = ArtifactRepository(store.root / "jobs" / ident).read(feedback["brief_ref"])
    assert brief["recommended_role"] == "coding" and len(json_text(brief).encode()) <= 8192


def test_budget_is_cumulative_and_does_not_start_model(repair):
    store, ident, _ = repair
    store.update(ident, elapsed_seconds=3600)
    coder = Coder()
    result = run_task(store, ident, Verifier(), coder)
    assert result["error_code"] == "budget_exhausted" and coder.calls == 0


def test_output_image_is_not_an_input_image_or_an_empty_goal():
    a = acceptance({"original_input": "Send an image", "requires_image": False, "goal_capabilities": ["image_delivery"]})
    assert a["original"] == "Send an image" and not a["requires_image"]
    assert {i["id"] for i in a["items"]} == {"expected_behavior", "image_materialized", "image_delivered"}
    assert not acceptance({"original_input": "Explain image module", "requires_image": False})["requires_image"]


def test_repeated_actions_stop_but_explicit_status_polls_do_not():
    event = {"type": "command_execution", "command": "probe", "exit_code": 1, "aggregated_output": "missing"}
    progress = ActionProgress()
    for _ in range(8):
        progress.observe({**event, "operation": "poll"})
    progress.observe(event)
    progress.observe(event)
    with pytest.raises(HarnessError, match="三次"):
        progress.observe(event)


def test_late_step_cannot_resurrect_cancelled_task(repair):
    store, ident, _ = repair
    step = {"id": "one", "status": "running", "parent_id": "attempt-1"}
    store.save_flow_step(ident, step)
    store.update(ident, status="cancelled")
    store.save_flow_step(ident, {**step, "status": "completed"})
    assert store.flow_steps(ident)[0]["status"] == "cancelled"


def test_rejected_identical_candidate_reuses_bound_review(repair):
    store, ident, _ = repair
    class Reviewer(Coder):
        reviews = 0
        def review(self, *args):
            self.reviews += 1
            return {"decision": "rejected", "problem": "missing negative case", "reason": "test coverage insufficient",
                    "evidence_refs": ["regression"]}
    coder = Reviewer()
    result = run_task(store, ident, Verifier(), coder)
    assert result["stop_reason"] == "no_progress"
    assert coder.calls == 2 and coder.reviews == 1
    assert store.attempts(ident)[-1]["review"]["cached"]


def test_mixed_validation_tracks_actual_external_child_until_confirmed(repair):
    store, ident, _ = repair
    class Pending(Verifier):
        def prepare(self, task, *args):
            source, plan = super().prepare(task, *args)
            return {**source, "test_sha256": "test", "agent_source": {"case_ids": ["agent"]}}, plan
        def run(self, task, candidate, evaluation_id, checks, cancel):
            assert store.get(task["task_id"])["current_evaluation_id"] == evaluation_id + "-agent"
            assert "current_evaluation_id" not in task
            raise HarnessError("evaluation_unavailable", "External completion unknown")
    result = run_task(store, ident, Pending(), Coder())
    assert result["status"] == "blocked"
    assert result["current_evaluation_id"].endswith("-reproduce-r1-agent")
    assert result["active_key"]
