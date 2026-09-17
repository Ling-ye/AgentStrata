"""Cancel delivery revalidation through real control/store/worker code and simulated providers."""
import sqlite3
from types import SimpleNamespace

import pytest

from chatcopilot.evals.service import EvaluationServiceUnavailable
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness import delivery, delivery_archive, delivery_runtime
from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
from chatcopilot.harness.models import HarnessError
from tests.unit.test_harness_lifecycle import Workers, occupied
from tests.unit import test_harness_pr_delivery as delivery_fixtures


task = delivery_fixtures.task


class EvaluationClient:
    def __init__(self):
        self.status = "running"
        self.mode = "unavailable"
        self.cancellations = []

    def get(self, _ident):
        return {"status": self.status}

    def cancel(self, ident):
        self.cancellations.append(ident)
        if self.mode == "lost_response":
            self.status = "cancelled"
        if self.mode in {"unavailable", "lost_response"}:
            raise EvaluationServiceUnavailable("controlled cancellation response unavailable")


def assemble(task, monkeypatch, source):
    store, ident, github, repository = task
    # A real local/bare-remote publication gives cancellation an existing PR to stop.
    delivery.reconcile(store, ident, client=github)
    reference = {"id": "eval-delivery-fixture", "digest": "fixture"}
    store.update(ident, source=source, delivery_evaluation=reference)
    store.interrupt(ident, storage_error=sqlite3.OperationalError("controlled storage failure"))
    external = EvaluationClient()
    evaluator = ServiceEvaluator(external)
    monkeypatch.setattr("chatcopilot.harness.api.configuration", lambda: {})
    controller = HarnessController(repository, root=store.root, evaluator=evaluator, worker_control=Workers())
    monkeypatch.setattr("chatcopilot.harness.evaluation_adapter.ServiceEvaluator", lambda: evaluator)
    monkeypatch.setattr("chatcopilot.harness.codex_adapter.CodexCoder", lambda *_: object())
    monkeypatch.setattr("chatcopilot.harness.local_verifier.LocalVerifier", lambda *_: object())
    monkeypatch.setattr("chatcopilot.harness.verification.CaseVerification", lambda *_: SimpleNamespace(evaluator=evaluator))
    monkeypatch.setattr(delivery_runtime, "reconcile", lambda store, ident, **kwargs:
                        delivery.reconcile(store, ident, client=github, **kwargs))
    return controller, external, reference


@pytest.mark.parametrize("source,suffix", [({"kind": "evaluation"}, ""),
    ({"kind": "robot_task", "test_sha256": "fixture", "agent_source": {"case_ids": ["one"]}}, "-agent")])
def test_storage_failure_then_cancel_keeps_delivery_reference_until_terminal(task, monkeypatch, source, suffix):
    store, ident, github, _ = task
    controller, external, reference = assemble(task, monkeypatch, source)
    assert controller.cancel(ident)["status"] == "blocked"
    assert store.get(ident)["delivery_evaluation"] == reference
    assert not store.get(ident).get("current_evaluation_id")
    assert occupied(controller, ident)
    assert ident in delivery_runtime.pending(store)
    with pytest.raises(HarnessError, match="暂不清理"):
        delivery_archive.cleanup_local(store, ident)

    for mode, code in (("unavailable", "evaluation_unavailable"), ("acknowledged", "result_pending")):
        external.mode = mode
        result = delivery_runtime.run_one(store, ident)
        assert result["status"] == "blocked"
        assert result["delivery"]["state"] == "cancel_pending"
        assert result["delivery"]["error_code"] == code
        assert store.get(ident)["delivery_request"] == "cancel"
        assert result["delivery_evaluation"] == reference
        assert occupied(controller, ident)
        assert ident in delivery_runtime.pending(store)
    assert external.cancellations == [reference["id"] + suffix] * 2

    external.status = "cancelled"
    delivery_runtime.run_one(store, ident)
    result = store.get(ident)
    assert result["status"] == "blocked"
    assert result["delivery"]["state"] == "cancelled"
    assert result["delivery_evaluation"] is None and result["delivery_request"] is None
    assert not occupied(controller, ident)
    assert github.pr["auto_merge"] is None
    assert github.creations == 1
    assert ident not in delivery_runtime.pending(store)
    # Repeated cancellation preserves the completed repair fact as well.
    assert controller.cancel(ident)["status"] == "blocked"


@pytest.mark.parametrize("mode", ["already_finished", "lost_response", "local_only"])
def test_delivery_cancel_handles_terminal_races_and_local_verification(task, monkeypatch, mode):
    store, ident, github, _ = task
    source = {"kind": "robot_task", **({"test_sha256": "fixture"} if mode == "local_only" else {})}
    controller, external, reference = assemble(task, monkeypatch, source)
    external.mode = mode
    if mode == "already_finished":
        external.status = "completed"
    controller.cancel(ident)
    result = delivery_runtime.run_one(store, ident)
    assert result["delivery_evaluation"] is None
    assert result["delivery"]["state"] == "cancelled"
    assert result["status"] == "blocked"
    assert external.cancellations == ([reference["id"]] if mode == "lost_response" else [])
    assert not occupied(controller, ident)
    assert github.pr["auto_merge"] is None


def test_unfinished_delivery_blocks_repair_resume_and_continuation(task, monkeypatch):
    store, ident, _, _ = task
    controller, _, reference = assemble(task, monkeypatch, {"kind": "robot_task"})
    before = store.get(ident)
    for operation in (controller.resume, controller.continue_task):
        with pytest.raises(HarnessError, match="交付复测尚未收尾"):
            operation(ident)
    assert store.get(ident) == before
    assert before["delivery_evaluation"] == reference
    assert occupied(controller, ident)


def test_repair_and_delivery_references_independently_reserve_task(task):
    store, ident, _, _ = task
    controller = SimpleNamespace(store=store)
    store.update(ident, current_evaluation_id="repair-eval", delivery_evaluation={"id": "delivery-eval", "digest": "fixture"})
    store.update(ident, current_evaluation_id=None)
    assert occupied(controller, ident)
    store.interrupt(ident, storage_error=sqlite3.OperationalError("controlled"))
    assert occupied(controller, ident)
    assert store.get(ident)["delivery_evaluation"]["id"] == "delivery-eval"
    store.update(ident, delivery_evaluation=None)
    assert not occupied(controller, ident)


def test_cancel_arriving_during_delivery_failure_remains_retryable(task, monkeypatch):
    store, ident, github, _ = task
    reference = {"id": "eval-inflight", "digest": "fixture"}
    store.update(ident, source={"kind": "robot_task"}, delivery_evaluation=reference)

    def pending_stop(*args, **kwargs):
        store.update(ident, delivery_cancel_requested=True, delivery_request="cancel")
        raise HarnessError("result_pending", "cancellation acknowledged, execution still running")

    monkeypatch.setattr(delivery, "_reconcile", pending_stop)
    result = delivery.reconcile(store, ident, client=github)
    assert result["delivery"]["state"] == "cancel_pending"
    assert result["delivery_evaluation"] == reference
    assert result["delivery_request"] == "cancel"
    assert ident in delivery_runtime.pending(store)
