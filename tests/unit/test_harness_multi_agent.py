"""Role scheduling, independent evidence, and host-owned acceptance."""
from dataclasses import asdict
import json

import pytest

from chatcopilot.harness.agent_types import AgentResult, Role
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.models import HarnessError, RepairFeedback, RepairOptions, RepairRequest
from chatcopilot.harness.repair_runtime import run_task
from test_harness_repair_v2 import Coder, Verifier, repair as repair_fixture


@pytest.fixture
def repair(tmp_path):
    return repair_fixture.__wrapped__(tmp_path)


class Roles(Coder):
    def __init__(self, *args, order="code_first", **kwargs):
        super().__init__(*args, **kwargs)
        self.roles = []
        self.evidence = []
        self.order = order

    def execute(self, root, call, options, output, cancel):
        self.roles.append(call.role)
        self.evidence.append((call.role, call.evidence))
        result = super().execute(root, call, options, output, cancel)
        if call.role == Role.PLAN:
            result.payload["verification_order"] = self.order
        return result


def test_host_advances_without_repeated_main_calls(repair):
    store, ident, _ = repair
    roles = Roles((2, 1))
    result = run_task(store, ident, Verifier(), roles)
    assert result["status"] == "fixed", result
    assert roles.roles == [Role.MAIN, Role.PLAN, Role.CODING, Role.TEST, Role.CODING, Role.REVIEW]
    assert result["accepted_candidate"]["candidate_digest"] == result["verified_digest"]
    assert len(store.attempts(ident)) == 2
    assert result["source_index_ref"]["kind"] == "source_index"
    assert store.attempts(ident)[0]["failure_brief"]["kind"] == "failure_brief"
    retry = [evidence for role, evidence in roles.evidence if role == Role.CODING][1]
    assert retry["failure_brief"]["recommended_role"] == "coding"
    assert "previous_failure" not in retry and "source_index" not in retry


def test_test_definition_retry_does_not_rewrite_product(repair):
    store, ident, _ = repair
    class BadFirstDefinition(Verifier):
        calls = 0
        def prepare(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise HarnessError("test_definition", "bad fixture")
            return super().prepare(*args)
    roles = Roles()
    result = run_task(store, ident, BadFirstDefinition(), roles)
    assert result["status"] == "fixed", result
    assert roles.roles.count(Role.CODING) == roles.roles.count(Role.MAIN) == 1
    assert roles.roles.count(Role.TEST) == 2


def test_invalid_test_first_still_retains_exploratory_candidate(repair):
    store, ident, _ = repair
    class InvalidTest(Roles):
        def execute(self, root, call, *args):
            if call.role == Role.TEST:
                self.roles.append(call.role)
                raise HarnessError("test_definition", "test is not executable")
            return super().execute(root, call, *args)
    roles = InvalidTest(order="test_first")
    result = run_task(store, ident, Verifier(), roles)
    assert roles.roles[:4] == [Role.MAIN, Role.PLAN, Role.TEST, Role.CODING]
    assert roles.calls == 1
    assert result["candidate_checkpoint"]["changed_files"]
    assert result["status"] != "fixed" and not result.get("accepted_candidate")


def test_main_cannot_dispatch_review_to_bypass_plan_or_checks(repair):
    store, ident, _ = repair
    class Bypass(Roles):
        def execute(self, root, call, *args):
            if call.role == Role.MAIN:
                return AgentResult({"next_role": "review", "summary": "ship now", "unresolved": []}, {})
            return super().execute(root, call, *args)
    roles = Bypass()
    result = run_task(store, ident, Verifier(), roles)
    assert roles.calls == 0 and result["status"] != "fixed"
    assert result["error_code"] in {"no_progress", "invalid_role_result"}


def test_plan_and_test_read_original_evidence_after_candidate_changes(repair):
    store, ident, _ = repair
    roles = Roles()
    result = run_task(store, ident, Verifier(), roles)
    repo = ArtifactRepository(store.root / "jobs" / ident)
    assert repo.read(result["problem_ref"]) == {"kind": "robot_task", "original_input": "Return one", "bot_id": "fixture"}
    steps = store.flow_steps(ident)
    role_steps = [s for s in steps if s.get("input", {}).get("role")]
    assert {s["input"]["role"] for s in role_steps} == {r.value for r in Role}
    for step in role_steps:
        assert repo.read(step["evidence"]["output"])
    with store.database.connect() as connection:
        payload = json.loads(connection.execute("SELECT payload FROM tasks WHERE task_id=?", (ident,)).fetchone()[0])
    assert "evaluations" not in payload and "evaluations" in payload["artifact_fields"]
    assert "original_input" not in payload["source"]


def test_artifact_reference_cannot_cross_tasks_or_hide_tampering(tmp_path):
    repo = ArtifactRepository(tmp_path / "task")
    ref = repo.put("plan", 1, {"summary": "original"})
    assert repo.read(ref)["summary"] == "original"
    with pytest.raises(HarnessError):
        repo.read({**asdict(ref), "path": "../elsewhere.json"})
    (repo.directory / ref.path).write_text('{"summary":"changed"}')
    with pytest.raises(HarnessError):
        repo.read(ref)


def test_evaluation_cannot_accept_manual_expected_answer():
    with pytest.raises(ValueError, match="人工答案"):
        RepairRequest("evaluation", RepairOptions("fixture"), "id", case_instance_id="case",
                      feedback=RepairFeedback(expected_behavior="replace the oracle"))
    assert RepairRequest("robot_task", RepairOptions("fixture"), "id", bot_id="bot", run_id="run").feedback == RepairFeedback()
    with pytest.raises(ValueError, match="时间预算"):
        RepairRequest("robot_task", RepairOptions("fixture", timeout_seconds=None), "id", bot_id="bot", run_id="run")


@pytest.mark.parametrize("decision,expected", [("proceed", "fixed"), ("blocked", "blocked")])
def test_plan_distinguishes_pending_evidence_from_required_blocker(repair, decision, expected):
    store, ident, _ = repair
    class PlanNotes(Roles):
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role == Role.PLAN:
                result.payload.update(decision=decision, unresolved=["Reproduction must still be confirmed by the host"])
            return result
    roles = PlanNotes()
    result = run_task(store, ident, Verifier(), roles)
    assert result["status"] == expected, result
    assert roles.calls == (1 if decision == "proceed" else 0)


def test_accepted_receipt_is_invalidated_by_changed_goal_items(repair):
    from chatcopilot.harness.delivery_candidate import accepted
    store, ident, _ = repair
    result = run_task(store, ident, Verifier(), Roles())
    assert accepted(store, result)
    # Keeping the original-text digest cannot hide changes to required checks.
    store.update(ident, acceptance={**result["acceptance"], "items": []})
    assert not accepted(store, store.get(ident))


def test_heartbeat_keeps_artifact_refs_without_reading_large_bodies(repair, monkeypatch):
    store, ident, _ = repair
    before = store.control_state(ident)["artifact_fields"]
    monkeypatch.setattr(ArtifactRepository, "read", lambda *args: pytest.fail("heartbeat hydrated a body"))
    store.heartbeat(ident, elapsed_seconds=12, remaining_seconds=3588)
    current = store.control_state(ident)
    assert current["elapsed_seconds"] == 12
    assert current["artifact_fields"] == before


def test_local_tool_warnings_are_notes_not_new_acceptance_goals(repair):
    store, ident, _ = repair
    class LocalWarning(Roles):
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role in {Role.CODING, Role.TEST}:
                result.payload["notes"] = ["A local pytest plugin could not create a socket; host verification is authoritative"]
            return result
    result = run_task(store, ident, Verifier(), LocalWarning())
    assert result["status"] == "fixed", result
    assert result["verification_gaps"] == []


@pytest.mark.parametrize("code", ["unverified", "permission_missing"])
def test_passing_checks_cannot_erase_reported_acceptance_gaps(repair, code):
    from test_harness_repair_v2 import proposal
    store, ident, _ = repair
    roles = Roles(proposed=proposal(gaps=[{"requirement": "expected_behavior", "code": code, "message": "needs evidence"}]))
    result = run_task(store, ident, Verifier(), roles)
    assert result["status"] == "needs_review", result


def test_partial_check_progress_allows_the_third_round(repair):
    from chatcopilot.harness.models import VerificationPlan, VerificationResult, VerificationCheck
    store, ident, _ = repair
    class TwoChecks(Verifier):
        def prepare(self, task, *args):
            return task["source"], VerificationPlan(("a", "b"), ("a", "b"), (), 1,
                coverage={"expected_behavior": ["a", "b"]})
        def run(self, task, candidate, ident, checks, cancel):
            value = int((candidate.path / "src/chatcopilot/core/probe.py").read_text().split("=")[1])
            return VerificationResult(ident, candidate.digest, tuple(
                VerificationCheck(name, 1, "passed" if value >= target else "failed", "" if value >= target else "product")
                for name, target in (("a", 3), ("b", 4))))
    roles = Roles((2, 3, 4))
    result = run_task(store, ident, TwoChecks(), roles)
    assert result["status"] == "fixed", result
    assert roles.calls == 3
