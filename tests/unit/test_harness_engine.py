"""Engine isolation and durable effect boundaries, with no model or platform calls."""

from dataclasses import asdict
import hashlib
import json
import subprocess
import sys

import pytest

from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine.contracts import VerificationBinding
from chatcopilot.harness.graph_repository import checkpoints
from chatcopilot.harness.engine.repair_graph import build_task_graph
from chatcopilot.harness.models import HarnessError, VerificationResult
from chatcopilot.harness.models import VerificationPlan
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.engine.evaluation import evaluate
from chatcopilot.harness.operation_repository import OperationRepository
from chatcopilot.harness.repair_repository import RepairArtifacts
from chatcopilot.harness.repair_runtime import run_task
from chatcopilot.harness.workflow import run_task as execute_core
from test_harness_repair_v2 import Coder, Verifier, proposal, repair as repair_fixture


@pytest.fixture
def repair(tmp_path):
    return repair_fixture.__wrapped__(tmp_path)


class ProcessStopped(BaseException):
    pass


class OpaqueScenario:
    purpose = "repair"

    def requirements(self, source):
        assert set(source) == {"opaque_report"}
        return {
            "original": "Return one",
            "sha256": hashlib.sha256(b"return-one").hexdigest(),
            "purpose": "repair",
            "requires_image": False,
            "items": [{"id": "expected_behavior", "required": True, "verification": "behavior"}],
        }

    def reproduction(self, source):
        return None

    def definition_source(self, task, proposed):
        return task["preparation_input"], [], self.requirements(task["preparation_input"])

    def retain_conditions(self, source, prior):
        return source

    def binding(self, task):
        return VerificationBinding(extensions={"report": task["source"]["opaque_report"]})

    def external_id(self, source, run_id):
        return None

    def review_extension(self, task, decision, review, baseline, candidate, paths):
        return decision

    def failure_brief(self, **kwargs):
        return {key: kwargs[key] for key in ("stage", "code", "message")}

    def classify_check(self, row):
        return "product"


class OpaqueRoles:
    def context(self, root, baseline, number, previous, capabilities):
        return {"route": "plan", "evidence": {}}

    def plan(self, root, number, context, options, cancel):
        return context

    def bind_plan(self, baseline, number, context):
        return {**context, "pending_roles": ["coding", "test"]}

    def call(self, role, root, number, goal, evidence, options, cancel):
        if role.value == "coding":
            (root / "src/chatcopilot/core/probe.py").write_text("VALUE = 1\n")
        return {"needs_replan": False}

    def submission(self, number, context):
        return proposal(), None

    def review(self, root, evidence, options, output, cancel):
        return {
            "decision": "approved",
            "problem": "",
            "reason": "observable result verified",
            "evidence_refs": ["verification", "patch"],
        }


def test_core_accepts_opaque_project_source_and_context_is_not_checkpointed(repair):
    store, ident, _ = repair
    store.update(ident, source={"opaque_report": "external-task-without-bot-or-case"})
    context = HarnessContext(
        store,
        ident,
        Verifier(),
        OpaqueScenario(),
        workspace_factory=lambda task: RepairArtifacts(store.root, task),
        roles_factory=lambda workspace: OpaqueRoles(),
    )
    assert execute_core(context)["status"] == "fixed"
    with checkpoints(store.root, "task:" + ident) as saver:
        snapshot = build_task_graph(saver).get_state(
            {"configurable": {"thread_id": "task:" + ident}}
        )
    payload = json.dumps(snapshot.values)
    assert snapshot.values["accepted_candidate_ref"]["sha256"]
    assert snapshot.values["candidate_ref"]["sha256"]
    assert "opaque_report" not in payload
    assert "verifier" not in snapshot.values and "scenario" not in snapshot.values
    assert (
        store.get(ident)["verification_binding"]["extensions"]["report"]
        == "external-task-without-bot-or-case"
    )


def test_core_import_does_not_load_project_or_backend_implementations():
    code = """
import sys
from chatcopilot.harness.engine.repair_graph import build_task_graph
build_task_graph(None)
forbidden = ('chatcopilot.harness.integrations', 'chatcopilot.harness.codex_adapter',
    'chatcopilot.harness.evaluation_adapter', 'chatcopilot.harness.worker_runtime',
    'chatcopilot.gateway', 'chatcopilot.evals', 'chatcopilot.agent')
assert not [name for name in sys.modules if name.startswith(forbidden)]
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_completed_operation_is_immutable_and_cannot_revive_cancelled_task(repair):
    store, ident, _ = repair
    records = OperationRepository(store, ident)
    records.start("role:plan-1", kind="role", node="plan", attempt=1, binding="frozen")
    store.update(ident, status="cancelled")
    ref = asdict(ArtifactRepository(store.root / "jobs" / ident).put("result", 1, {"done": True}))
    records.complete("role:plan-1", "frozen", ref)
    assert store.get(ident)["status"] == "cancelled"
    assert records.get("role:plan-1")["state"] == "completed"
    with pytest.raises(HarnessError, match="冻结输入"):
        records.start("role:plan-1", kind="role", node="plan", attempt=1, binding="changed")
    with pytest.raises(HarnessError, match="不能被覆盖"):
        records.complete("role:plan-1", "frozen", {**ref, "sha256": "changed"})


def test_public_projection_hides_current_and_archived_operation_receipts():
    from chatcopilot.harness.api import HarnessController
    from chatcopilot.harness.models import PIPELINE_VERSION

    private = (
        "operations",
        "verification_binding",
        "role_executions",
        "delivery_acceptance_receipts",
    )
    for version in (PIPELINE_VERSION, PIPELINE_VERSION - 1):
        task = {
            "task_id": "repair-fixture",
            "pipeline_version": version,
            "source": {},
            **{key: {"private_receipt": "retained"} for key in private},
        }
        public = HarnessController._public(task)
        assert not set(private).intersection(public)
        assert public["archived"] is (version != PIPELINE_VERSION)


def test_unknown_local_verification_is_not_blindly_repeated(repair):
    store, ident, _ = repair
    # A prepared local verifier has a test digest and no external Evaluation ID.
    source = store.get(ident)["source"]
    store.update(ident, source={**source, "test_sha256": "frozen-local-test"})

    class InterruptedVerifier(Verifier):
        calls = 0

        def run(self, task, candidate, run_id, checks, cancel):
            result = super().run(task, candidate, run_id, checks, cancel)
            if run_id.endswith("verify-1"):
                self.calls += 1
                raise ProcessStopped()
            return result

    verifier = InterruptedVerifier()
    with pytest.raises(ProcessStopped):
        run_task(store, ident, verifier, Coder())
    assert store.get(ident)["operations"]["verification:verify-1"]["state"] == "started"
    store.update(ident, status="queued")
    result = run_task(store, ident, verifier, Coder())
    assert result["status"] == "blocked" and result["error_code"] == "result_pending"
    assert verifier.calls == 1 and not result.get("accepted_candidate")


@pytest.mark.parametrize("condition_drift", ["none", "conditions", "model"])
def test_durable_verification_result_survives_loss_of_node_checkpoint(
    repair, monkeypatch, condition_drift
):
    store, ident, _ = repair
    original = OperationRepository.complete
    stopped = False

    def stop_after_result(self, key, *args, **kwargs):
        nonlocal stopped
        result = original(self, key, *args, **kwargs)
        if key == "verification:verify-1" and not stopped:
            stopped = True
            raise ProcessStopped()
        return result

    monkeypatch.setattr(OperationRepository, "complete", stop_after_result)
    verifier, coder = Verifier(), Coder()
    with pytest.raises(ProcessStopped):
        run_task(store, ident, verifier, coder)
    store.update(ident, status="queued")
    if condition_drift == "conditions":
        source = store.get(ident)["source"]
        store.update(ident, source={**source, "conditions": {"changed": True}})
    elif condition_drift == "model":
        options = store.get(ident)["options"]
        store.update(ident, options={**options, "model": "changed-fixture-model"})
    result = run_task(store, ident, verifier, coder)
    if condition_drift != "none":
        expected = "operation_changed" if condition_drift == "model" else "verification_evidence"
        assert result["status"] == "blocked" and result["error_code"] == expected
    else:
        assert result["status"] == "fixed", result.get("message")
    assert len(verifier.paths) == 2 and coder.calls == 1


def test_result_from_another_verification_identity_is_rejected(repair):
    store, ident, _ = repair

    class WrongIdentity(Verifier):
        def run(self, *args):
            result = super().run(*args)
            return VerificationResult("another-run", result.candidate_digest, result.checks)

    result = run_task(store, ident, WrongIdentity(), Coder())
    assert result["status"] == "blocked" and result["error_code"] == "verification_evidence"
    assert not result.get("accepted_candidate")


@pytest.mark.parametrize(
    "code", ["evaluation_incomplete", "incomplete_verification", "source_mismatch"]
)
def test_verification_service_faults_stop_before_another_product_edit(repair, code):
    store, ident, _ = repair

    class Unavailable(Verifier):
        def run(self, *args):
            raise HarnessError(code, "verification did not establish product behavior")

    coder = Coder()
    result = run_task(store, ident, Unavailable(), coder)
    assert result["status"] == "blocked" and result["error_code"] == code
    assert coder.calls == 1 and len(store.attempts(ident)) == 1
    assert not result.get("accepted_candidate")


def test_baseline_reuse_requires_a_completed_receipt_not_a_display_projection(repair):
    store, ident, _ = repair
    source = {"opaque_report": "external-task-without-bot-or-case"}
    scenario, verifier = OpaqueScenario(), Verifier()
    store.update(ident, source=source, acceptance=scenario.requirements(source), status="running")
    context = HarnessContext(
        store,
        ident,
        verifier,
        scenario,
        workspace_factory=lambda task: RepairArtifacts(store.root, task),
        roles_factory=lambda workspace: OpaqueRoles(),
    )
    candidate = context.baseline_candidate
    plan = VerificationPlan(("target",), ("target",), (), 1)
    store.update(ident, verification_plan=plan.to_payload())
    task = store.get(ident)
    identity = hashlib.sha256(
        json_text(
            {
                "source": candidate.digest,
                "plan": plan.to_payload(),
                "binding": asdict(scenario.binding(task)),
                "environment": task.get("environment"),
            }
        ).encode()
    ).hexdigest()
    invented = {
        "evaluation_id": "unconfirmed",
        "complete": True,
        "baseline_identity": identity,
        "passed_cases": ["target"],
        "failed_cases": [],
        "result_ref": context.put("projection", 1, {}),
    }
    store.update(ident, evaluations={"reproduce-r1": invented})
    with context.budget:
        result = evaluate(context, candidate, plan, "reproduce-r2", ["target"])
    assert len(verifier.paths) == 1
    assert result["failed_cases"] == ["target"] and "reused_from" not in result
