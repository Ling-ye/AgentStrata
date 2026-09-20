"""Role scheduling, independent evidence, and host-owned acceptance."""
from dataclasses import asdict
import json

import pytest

from chatcopilot.harness.agent_types import AgentResult, Role
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.models import HarnessError, RepairFeedback, RepairOptions, RepairRequest
from chatcopilot.harness.repair_runtime import run_task
from test_harness_repair_v2 import Coder, Verifier, repair as repair_fixture


def test_role_output_schemas_require_every_declared_property():
    from chatcopilot.harness.agent_types import role_schema
    def check(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert set(value["required"]) == set(value["properties"])
                assert value["additionalProperties"] is False
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    for governance in (False, True):
        for role in Role:
            check(role_schema(role, governance=governance))


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
    plan_input = next(evidence for role, evidence in roles.evidence if role == Role.PLAN)
    assert plan_input["source"]["runtime_entrypoint"] == "src/chatcopilot/gateway/runtime.py"
    assert store.attempts(ident)[0]["failure_brief"]["kind"] == "failure_brief"
    retry = [evidence for role, evidence in roles.evidence if role == Role.CODING][1]
    assert retry["failure_brief"]["recommended_role"] == "coding"
    assert "previous_failure" not in retry and "source_index" not in retry


def test_invalid_baseline_trials_keep_original_error_and_stop_reason_in_brief(repair):
    from chatcopilot.harness.models import VerificationCheck, VerificationResult
    store, ident, _ = repair
    class UnavailableModel(Verifier):
        def run(self, task, candidate, run_id, checks, cancel):
            return VerificationResult(run_id, candidate.digest, (VerificationCheck(
                "target", 1, "error", "environment", {"error": {"message": "missing model configuration"}}),))
    result = run_task(store, ident, UnavailableModel(), Roles())
    assert result["error_code"] == "verification_environment"
    attempt = store.attempts(ident)[0]
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    brief = artifacts.read(attempt["failure_brief"])
    assert brief["recommended_role"] == "stop"
    assert "missing model configuration" in brief["diagnostics"][0]["text"]
    reference = brief["diagnostics"][0]["result_ref"]
    assert reference == attempt["reproduction"]["result_ref"]
    assert artifacts.read(reference)["checks"][0]["failure_kind"] == "environment"
    assert brief["comparison"][0]["baseline"] == {"passed": 0, "total": 1}


class ReplanningRoles(Roles):
    def execute(self, root, call, *args):
        result = super().execute(root, call, *args)
        if call.role == Role.PLAN and call.evidence.get("rediagnosis"):
            ref = call.evidence["failure_brief"]["evidence_refs"][0]
            result.payload.update(changes=["Correct the value selected by the recorded failing target"],
                                  evidence_refs=[ref["path"] + "#/checks/0"], next_role="coding")
        return result


def test_repeated_failure_replans_once_using_remaining_attempt(repair):
    store, ident, _ = repair
    roles = ReplanningRoles((2, 3, 1))
    result = run_task(store, ident, Verifier(), roles)
    assert result["status"] == "fixed", result.get("message")
    assert roles.roles.count(Role.PLAN) == 2
    assert roles.roles.count(Role.MAIN) == 1
    assert roles.roles.count(Role.TEST) == 1
    assert store.attempts(ident)[1]["feedback"]["rediagnosis"]
    assert result["evaluations"]["reproduce-r3"]["reused_from"].endswith("reproduce-r1")


def test_paraphrased_plan_without_bound_evidence_stops_before_third_edit(repair):
    store, ident, _ = repair
    roles = Roles((2, 3, 1))
    result = run_task(store, ident, Verifier(), roles)
    assert result["error_code"] == "no_progress"
    assert roles.calls == 2 and roles.roles.count(Role.PLAN) == 2


def test_no_second_rediagnosis_even_with_extra_attempt_budget(repair):
    store, ident, _ = repair
    store.update(ident, options=asdict(RepairOptions("fixture", max_attempts=5)))
    roles = ReplanningRoles((2, 3, 4, 1))
    result = run_task(store, ident, Verifier(), roles)
    assert result["error_code"] == "no_progress"
    assert roles.calls == 3 and roles.roles.count(Role.PLAN) == 2


def test_repair_scope_keeps_permission_authority_read_only(tmp_path):
    from chatcopilot.harness.workspace import permitted_change, protected_paths, writable_paths
    names = ["src/chatcopilot/authorization/policy.py", "src/chatcopilot/contracts/execution_scope.py"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixed host policy\n")
    assert all(tmp_path / name in protected_paths(tmp_path) for name in names)
    assert all(tmp_path / name not in writable_paths(tmp_path) for name in names)
    assert not any(permitted_change(name) for name in names)
    assert not permitted_change("src/chatcopilot/harness/workflow.py")
    assert permitted_change("src/chatcopilot/contracts/agent.py")


def test_repeated_definition_failure_can_replan_test_without_rewriting_product(repair):
    store, ident, _ = repair
    class Definitions(Verifier):
        calls = 0
        def prepare(self, *args):
            self.calls += 1
            if self.calls < 3:
                error = HarnessError("verification_test_definition", "wrong fixture API")
                error.evidence = {"result": {"rows": {"test_target": {"outcome": "error", "message": "KeyError: field"}}}}
                raise error
            return super().prepare(*args)
    class RolesWithTestReplan(Roles):
        def execute(self, root, call, *args):
            result = super().execute(root, call, *args)
            if call.role == Role.PLAN and call.evidence.get("rediagnosis"):
                ref = call.evidence["failure_brief"]["evidence_refs"][0]
                result.payload.update(changes=["Use the actual fixture field"], next_role="test",
                                      evidence_refs=[ref["path"] + "#/result/rows/test_target"])
            return result
    roles = RolesWithTestReplan()
    result = run_task(store, ident, Definitions(), roles)
    assert result["status"] == "fixed", result.get("message")
    assert roles.calls == 1 and roles.roles.count(Role.TEST) == 3
    assert roles.roles.count(Role.PLAN) == 2


def test_test_definition_retry_does_not_rewrite_product(repair):
    store, ident, _ = repair
    class BadFirstDefinition(Verifier):
        calls = 0
        def prepare(self, *args):
            self.calls += 1
            if self.calls == 1:
                error = HarnessError("verification_test_definition", "bad fixture")
                error.evidence = {"phase": "definition", "result": {"rows": {
                    "test_target": {"outcome": "failed", "message": "KeyError: 'failure_category'"}}, "errors": []}}
                raise error
            return super().prepare(*args)
    roles = Roles()
    result = run_task(store, ident, BadFirstDefinition(), roles)
    assert result["status"] == "fixed", result
    assert roles.roles.count(Role.CODING) == roles.roles.count(Role.MAIN) == 1
    assert roles.roles.count(Role.TEST) == 2
    retry = [evidence for role, evidence in roles.evidence if role == Role.TEST][1]
    brief = retry["failure_brief"]
    assert brief["failed_checks"] == ["test_target"]
    assert brief["diagnostics"] == [{"check": "test_target", "text": "KeyError: 'failure_category'"}]
    assert brief["evidence_refs"][0]["kind"] == "verification_error"
    artifacts = ArtifactRepository(store.root / "jobs" / ident)
    assert artifacts.read(brief["evidence_refs"][0])["result"]["rows"]["test_target"]["outcome"] == "failed"


def test_repair_feedback_remains_visible_when_original_navigation_is_omitted(repair):
    from chatcopilot.harness.evidence_context import evidence_index
    store, ident, _ = repair
    feedback = {"repair_hint": "Check the actual Gateway outbox before changing the transport."}
    store.update(ident, source={**store.get(ident)["source"], "feedback": feedback, "extra_notes": "x" * 4000})
    roles = Roles()
    result = run_task(store, ident, Verifier(), roles)
    assert result["status"] == "fixed", result
    for role, evidence in roles.evidence:
        if role not in {Role.PLAN, Role.CODING, Role.TEST}:
            continue
        context = evidence_index(evidence, stage="prepare" if role != Role.CODING else "repair")
        assert "original_source" in context["omitted_sections"]
        assert context["inline_sections"]["feedback"] == feedback


def test_continuation_exposes_prior_draft_without_reusing_old_acceptance(repair):
    from chatcopilot.harness.evidence_context import evidence_index
    store, ident, _ = repair
    material = {"test": "# previous draft\n" * 1000, "checks": [{"rows": {"old": {"outcome": "passed"}}}]}
    store.update(ident, prior_material=material, prior_evaluations={"verify-1": {"complete": True, "passed_cases": ["target"]}})
    light = store.control_state(ident)
    assert "prior_material" not in light and "prior_evaluations" not in light
    assert {"prior_material", "prior_evaluations"}.issubset(light["artifact_fields"])
    roles, verifier = Roles(), Verifier()
    result = run_task(store, ident, verifier, roles)
    assert result["status"] == "fixed", result
    assert roles.calls == 1 and len(verifier.paths) == 2
    supplied = next(evidence for role, evidence in roles.evidence if role == Role.TEST)
    reference = evidence_index(supplied, stage="prepare")["inline_sections"]["prior_validation"]
    assert reference["sections"]["test"]["pointer"] == "/test"
    assert reference["sha256"] == light["artifact_fields"]["prior_material"]["sha256"]


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


@pytest.mark.parametrize("code", ["fixture_missing", "material_missing", "permission_missing"])
def test_passing_checks_cannot_erase_reported_acceptance_gaps(repair, code):
    from test_harness_repair_v2 import proposal
    store, ident, _ = repair
    roles = Roles(proposed=proposal(gaps=[{"requirement": "expected_behavior", "code": code, "message": "needs evidence"}]))
    result = run_task(store, ident, Verifier(), roles)
    assert result["status"] == "needs_review", result


@pytest.mark.parametrize("role", [Role.CODING, Role.TEST])
@pytest.mark.parametrize("governance", [False, True])
def test_pending_host_verification_is_not_a_role_prerequisite_gap(role, governance):
    from chatcopilot.harness.agent_types import role_result
    from test_harness_repair_v2 import proposal
    gap = {"requirement": "expected_behavior", "code": "unverified", "message": "awaiting host checks"}
    payload = ({"summary": "candidate ready", "notes": [], "needs_replan": False, "gaps": [gap]}
               if role == Role.CODING else proposal(gaps=[gap]))
    with pytest.raises(HarnessError, match="完整结构化产物"):
        role_result(role, payload, governance=governance)
    payload["gaps"] = []
    payload["notes"] = ["awaiting host checks"]
    assert role_result(role, payload, governance=governance)["notes"] == ["awaiting host checks"]


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


@pytest.mark.parametrize("improves", [True, False])
def test_draft_progress_uses_valid_behavior_checks_not_changing_filenames(repair, improves):
    store, ident, _ = repair

    class DraftVerifier(Verifier):
        calls = 0

        def prepare(self, *args):
            self.calls += 1
            if self.calls < 3:
                error = HarnessError("verification_test_definition", "draft still has an invalid check")
                prefix = f"tests/unit/harness_regressions/test_{self.calls}.py::"
                error.evidence = {"result": {"rows": {
                    prefix + "test_control": {"outcome": "passed"},
                    prefix + "test_primary": {"outcome": "failed", "assertion_failure": improves and self.calls == 2},
                    prefix + "test_auxiliary": {"outcome": "failed", "exception_chain": [{"type": "KeyError"}]}}}}
                raise error
            return super().prepare(*args)

    roles = Roles()
    result = run_task(store, ident, DraftVerifier(), roles)
    assert result["status"] == ("fixed" if improves else "blocked"), result
    assert roles.roles.count(Role.CODING) == 1
    assert roles.roles.count(Role.TEST) == (3 if improves else 2)
    assert store.attempts(ident)[0]["valid_definition_checks"] == ["test_control"]
    assert store.attempts(ident)[1]["valid_definition_checks"] == (["test_control", "test_primary"] if improves else ["test_control"])
    if not improves:
        assert result["error_code"] == "no_progress"
