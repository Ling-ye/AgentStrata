"""A blocked role can learn the host schema without receiving an acceptance waiver."""

import json
from types import SimpleNamespace

import pytest

from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.verification_retry_runtime import inspect_blocked_agent_draft
from test_harness_confirmation_retry import controller_for
from test_harness_repair_v2 import Coder, Verifier, proposal, repair as repair_fixture
from chatcopilot.harness.repair_runtime import run_task


@pytest.fixture
def blocked(tmp_path):
    store, ident, repo = repair_fixture.__wrapped__(tmp_path)
    gaps = [{"requirement": "expected_behavior", "code": "fixture_missing", "message": "old local schema"}]
    task = run_task(store, ident, Verifier(), Coder(proposed=proposal(
        decision="blocked", verification_kind="agent", gaps=gaps)))
    store.update(ident, source={**task["source"], "verification_runtime": "a" * 64})
    draft = store.root / "jobs" / ident / "attempt-1/draft"
    draft.mkdir(exist_ok=True, mode=0o700)
    path = draft / "agent_case.json"
    path.write_text(json.dumps({"input": "business input"}))
    path.chmod(0o600)
    return store, ident, repo


def test_resume_records_schema_only_evidence_and_routes_test_without_accepting(blocked):
    store, ident, repo = blocked
    before = store.get(ident)
    old = store.attempts(ident)[0]
    seen = []
    controller = controller_for(store, repo)
    controller.evaluator = SimpleNamespace(
        verification_runtime="a" * 64,
        validate_agent_case=lambda case: seen.append(case) or case,
    )
    controller.resume(ident)
    current = store.get(ident)
    assert current["status"] == "queued" and not current.get("accepted_candidate")
    assert current["elapsed_seconds"] == before["elapsed_seconds"]
    assert current["options"] == before["options"]
    attempt = store.attempts(ident)[0]
    assert all(attempt[key] == value for key, value in old.items())
    assert attempt["feedback"]["stage"] == "definition"
    records = ArtifactRepository(store.root / "jobs" / ident)
    proof = records.read(attempt["definition_inspection"])
    assert proof["scope"] == "schema_only" and proof["product_verified"] is False
    assert seen == [{"input": "business input", "runtime_replay": True,
                     "expected_behavior": before["acceptance"]["original"]}]
    assert records.read(attempt["feedback"]["brief_ref"])["recommended_role"] == "test"
    # Rechecking is idempotent and never alters the original blocked submission.
    inspect_blocked_agent_draft(store, ident, controller.evaluator)
    assert len(seen) == 1 and store.attempts(ident)[0] == attempt


@pytest.mark.parametrize("problem", ["wrong_runtime", "unsupported", "linked_draft"])
def test_inspection_cannot_supply_positive_evidence_from_an_invalid_boundary(blocked, problem):
    store, ident, _ = blocked
    old = store.attempts(ident)[0]

    def validate(case):
        if problem == "unsupported":
            raise HarnessError("fixture_missing", "missing host fixture")
        return case

    evaluator = SimpleNamespace(verification_runtime="b" * 64 if problem == "wrong_runtime" else "a" * 64,
                                validate_agent_case=validate)
    if problem == "linked_draft":
        draft = store.root / "jobs" / ident / "attempt-1/draft/agent_case.json"
        draft.with_suffix(".linked").hardlink_to(draft)
    with pytest.raises((HarnessError, ValueError)):
        inspect_blocked_agent_draft(store, ident, evaluator)
    assert store.attempts(ident)[0] == old
    assert store.get(ident)["status"] == "blocked"


def test_unbound_historical_task_is_not_reinterpreted(blocked):
    store, ident, _ = blocked
    task = store.get(ident)
    store.update(ident, source={k: v for k, v in task["source"].items() if k != "verification_runtime"})
    old = store.attempts(ident)[0]
    inspect_blocked_agent_draft(store, ident, SimpleNamespace())
    assert store.attempts(ident)[0] == old
