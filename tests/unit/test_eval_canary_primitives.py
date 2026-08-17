from __future__ import annotations

import ast
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from chatcopilot.evals.canary import (
    CanaryConflictError,
    CanaryDeploymentLease,
    CanaryIntegrityError,
    CanaryLeaseStore,
    CanaryPhase,
    CanaryReceiptVerifier,
    CanaryReceiptWriter,
    CanarySafetyError,
    CanaryStateError,
    CanaryStateMachine,
    CanaryTargetFactory,
    GenerationStore,
    LeaseState,
    ProductionFingerprint,
    QuarantineScope,
    decide_quarantine,
)


HANDLE_KEY = b"handle-key-for-tests-32-bytes!!!"
LEASE_KEY = b"lease-key-for-tests--32-bytes!!!"
RECEIPT_KEY = b"receipt-key-for-tests-32-bytes!!"


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "canary-private"
    root.mkdir(mode=0o700)
    return root


def _target(tmp_path: Path):
    root = _private_root(tmp_path)
    production = tmp_path / "production-runtime"
    # Keep the rejected production-unit fixture visibly synthetic without
    # committing an email-shaped token to the public repository.
    production_unit = "chatcopilot" + "@" + "production.service"
    factory = CanaryTargetFactory(
        root,
        production_fingerprints=(
            ProductionFingerprint(
                roots=(production,),
                sockets=(tmp_path / "production.sock",),
                units=(production_unit,),
            ),
        ),
        handle_key=HANDLE_KEY,
    )
    handle = factory.create_target(
        evaluation_id="eval-canary-1",
        trial_id="trial-1",
        template_id="self-update-fixture-v1",
    )
    return factory, handle


def test_target_factory_creates_only_private_disjoint_paths(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)

    assert factory.validate_handle(handle) == handle
    assert handle.unit_name.startswith("chatcopilot-canary@")
    assert handle.unit_name.endswith(".service")
    assert handle.target_root.parent == factory.private_root
    for path in (
        handle.target_root,
        handle.source_base,
        handle.source_work,
        handle.releases_root,
        handle.workspace_root,
        handle.sockets_root,
        handle.control_root,
        handle.receipts_root,
        handle.quarantine_root,
    ):
        assert path.is_dir()
        assert not path.is_symlink()
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o777 == 0o700


def test_target_factory_rejects_unsafe_root_and_production_overlap(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    with pytest.raises(CanarySafetyError, match="0700"):
        CanaryTargetFactory(unsafe, handle_key=HANDLE_KEY)

    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    symlink = tmp_path / "symlink-root"
    symlink.symlink_to(actual, target_is_directory=True)
    with pytest.raises(CanarySafetyError, match="real directory"):
        CanaryTargetFactory(symlink, handle_key=HANDLE_KEY)

    overlap_parent = tmp_path / "overlap-case"
    overlap_parent.mkdir()
    overlapping = _private_root(overlap_parent)
    with pytest.raises(CanarySafetyError, match="production fingerprint"):
        CanaryTargetFactory(
            overlapping,
            production_fingerprints=(
                ProductionFingerprint(roots=(overlapping / "production-child",)),
            ),
            handle_key=HANDLE_KEY,
        )


def test_forged_target_handle_is_rejected_before_use(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    forged = replace(handle, target_root=tmp_path / "production-runtime")

    with pytest.raises(CanaryIntegrityError, match="seal"):
        factory.validate_handle(forged)


def test_generation_stage_activate_and_guarded_restore(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    store = GenerationStore(factory, handle)
    baseline = store.stage(
        "baseline-v1",
        {
            "canary_version.py": "VALUE = 'baseline-v1'\n",
            "nested/config.json": b"{}\n",
        },
    )
    candidate = store.stage(
        "candidate-v2",
        {"canary_version.py": "VALUE = 'candidate-v2'\n"},
    )

    active = store.activate("baseline-v1", expected_current=None)
    assert active.active_generation == "baseline-v1"
    assert active.generation_digest == baseline.digest

    active = store.activate("candidate-v2", expected_current="baseline-v1")
    assert active.active_generation == "candidate-v2"
    assert active.previous_generation == "baseline-v1"
    assert active.generation_digest == candidate.digest

    with pytest.raises(CanaryConflictError, match="drifted"):
        store.restore("baseline-v1", expected_active_generation="unexpected")
    assert store.current_activation().active_generation == "candidate-v2"  # type: ignore[union-attr]

    restored = store.restore("baseline-v1", expected_active_generation="candidate-v2")
    assert restored.active_generation == "baseline-v1"
    assert restored.previous_generation == "candidate-v2"


def test_generation_rejects_traversal_symlink_and_content_drift(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    store = GenerationStore(factory, handle)
    with pytest.raises(CanarySafetyError, match="invalid Canary generation path"):
        store.stage("bad-path", {"../production": "no"})

    descriptor = store.stage("candidate-v2", {"canary.py": "candidate-v2\n"})
    candidate_file = descriptor.root / "canary.py"
    candidate_file.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(CanaryIntegrityError, match="drifted"):
        store.verify_generation("candidate-v2")

    linked = store.stage("linked-v3", {"canary.py": "linked\n"})
    source = linked.root / "canary.py"
    os.link(source, linked.root / "second-link")
    with pytest.raises(CanarySafetyError, match="one hard link"):
        store.verify_generation("linked-v3")


def test_activation_descriptor_rejects_symlink(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    store = GenerationStore(factory, handle)
    store.stage("baseline-v1", {"canary.py": "baseline\n"})
    production_file = tmp_path / "production-activation.json"
    production_file.write_text("{}", encoding="utf-8")
    store.activation_path.symlink_to(production_file)

    with pytest.raises(CanarySafetyError, match="regular file"):
        store.activate("baseline-v1", expected_current=None)
    assert production_file.read_text(encoding="utf-8") == "{}"


def _lease(factory, handle, *, now: str = "2026-08-17T00:00:00+00:00"):
    store = GenerationStore(factory, handle)
    baseline = store.stage("baseline-v1", {"canary.py": "baseline\n"})
    candidate = store.stage("candidate-v2", {"canary.py": "candidate\n"})
    lease = CanaryDeploymentLease.create(
        handle=handle,
        observer_unit=handle.unit_name,
        observer_pid=1234,
        observer_start_time="987654",
        baseline_generation=baseline.generation_id,
        candidate_generation=candidate.generation_id,
        candidate_digest=candidate.digest,
        now=now,
    )
    return CanaryLeaseStore(factory, handle, signing_key=LEASE_KEY), lease


def test_deployment_lease_is_signed_exclusive_and_stateful(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    store, lease = _lease(factory, handle)
    current = store.acquire(lease)
    assert current.state == LeaseState.LEASED
    assert store.path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(CanaryConflictError, match="already active"):
        store.acquire(replace(lease, lease_id="f" * 32))

    for state in (
        LeaseState.ACTIVATING,
        LeaseState.VERIFYING,
        LeaseState.RESTORING,
        LeaseState.CLEANUP,
    ):
        current = store.transition(current.lease_id, state)
        assert current.state == state

    with pytest.raises(CanaryStateError, match="invalid Canary lease transition"):
        store.transition(current.lease_id, LeaseState.ACTIVATING)
    store.release(current.lease_id)
    assert not store.path.exists()


def test_deployment_lease_tamper_and_stale_takeover_are_rejected(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    store, lease = _lease(factory, handle)
    active = store.acquire(lease)

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["candidate_digest"] = "f" * 64
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CanaryIntegrityError, match="signature"):
        store.load()

    # Restoring the valid signed payload still does not allow time-based stealing.
    store.path.unlink()
    active = store.acquire(active)
    with pytest.raises(CanaryConflictError, match="already active"):
        store.acquire(replace(lease, lease_id="e" * 32, last_heartbeat_at="2000-01-01"))


def _write_success_receipts(writer: CanaryReceiptWriter) -> None:
    phases = (
        CanaryPhase.PREPARED,
        CanaryPhase.BASELINE_VERIFIED,
        CanaryPhase.AGENT_TURN_COMPLETED,
        CanaryPhase.LIFECYCLE_REQUESTED,
        CanaryPhase.CANDIDATE_VALIDATED,
        CanaryPhase.ACTIVATED,
        CanaryPhase.RESTART_OBSERVED,
        CanaryPhase.CANDIDATE_BEHAVIOR_VERIFIED,
        CanaryPhase.RESTORING,
        CanaryPhase.BASELINE_RESTORED,
        CanaryPhase.CLEANUP_COMPLETED,
    )
    for index, phase in enumerate(phases, start=1):
        writer.append(
            phase,
            {"check": True, "phase_index": index},
            observed_at=f"2026-08-17T00:00:{index:02d}+00:00",
        )


def test_receipt_chain_is_ordered_identity_bound_and_signed(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    writer = CanaryReceiptWriter(factory, handle, signing_key=RECEIPT_KEY)
    _write_success_receipts(writer)

    receipts = CanaryReceiptVerifier(
        factory,
        handle,
        signing_key=RECEIPT_KEY,
    ).verify()
    assert len(receipts) == 11
    assert receipts[0].previous_digest == ""
    assert receipts[-1].phase == CanaryPhase.CLEANUP_COMPLETED
    assert receipts[-1].previous_digest == receipts[-2].digest

    with pytest.raises(CanaryIntegrityError, match="signature"):
        CanaryReceiptVerifier(factory, handle, signing_key=b"wrong-key" * 4).verify()


def test_receipt_chain_rejects_tampering_and_invalid_transition(tmp_path: Path) -> None:
    factory, handle = _target(tmp_path)
    writer = CanaryReceiptWriter(factory, handle, signing_key=RECEIPT_KEY)
    with pytest.raises(CanaryStateError, match="invalid Canary transition"):
        writer.append(CanaryPhase.CANDIDATE_VALIDATED, {"skipped": True})
    assert list(handle.receipts_root.iterdir()) == []

    writer.append(CanaryPhase.PREPARED, {"safe": True})
    path = next(handle.receipts_root.iterdir())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"]["safe"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CanaryIntegrityError, match="digest"):
        CanaryReceiptVerifier(factory, handle, signing_key=RECEIPT_KEY).verify()


def test_state_machine_requires_ordered_restore_and_cleanup() -> None:
    machine = CanaryStateMachine()
    with pytest.raises(CanaryStateError):
        machine.advance(CanaryPhase.ACTIVATED)
    assert machine.advance(CanaryPhase.PREPARED) == CanaryPhase.PREPARED
    assert machine.advance(CanaryPhase.RESTORING) == CanaryPhase.RESTORING
    assert machine.advance(CanaryPhase.BASELINE_RESTORED) == CanaryPhase.BASELINE_RESTORED
    assert machine.advance(CanaryPhase.CLEANUP_COMPLETED) == CanaryPhase.CLEANUP_COMPLETED
    with pytest.raises(CanaryStateError):
        machine.advance(CanaryPhase.QUARANTINED)


@pytest.mark.parametrize(
    ("arguments", "scope", "reason"),
    (
        (
            {
                "mutation_started": True,
                "production_unchanged": None,
                "paths_contained": True,
                "observer_identity_proven": True,
                "active_generation_known": True,
                "baseline_restored": True,
                "unit_stopped": True,
            },
            QuarantineScope.SUBSYSTEM,
            "production_unchanged_not_proven",
        ),
        (
            {
                "mutation_started": True,
                "production_unchanged": True,
                "paths_contained": True,
                "observer_identity_proven": None,
                "active_generation_known": True,
                "baseline_restored": False,
                "unit_stopped": False,
            },
            QuarantineScope.TARGET,
            "observer_identity_not_proven",
        ),
        (
            {
                "mutation_started": True,
                "production_unchanged": True,
                "paths_contained": True,
                "observer_identity_proven": True,
                "active_generation_known": True,
                "baseline_restored": True,
                "unit_stopped": True,
            },
            QuarantineScope.NONE,
            None,
        ),
    ),
)
def test_quarantine_decision_is_fail_closed(arguments, scope, reason) -> None:
    decision = decide_quarantine(**arguments)
    assert decision.scope == scope
    if reason:
        assert reason in decision.reasons
    else:
        assert decision.reasons == ()


def test_canary_package_has_no_process_git_or_network_imports() -> None:
    package_root = Path(__file__).parents[2] / "src/chatcopilot/evals/canary"
    forbidden = {"subprocess", "socket", "requests", "httpx", "git"}
    observed: set[str] = set()
    for source_path in package_root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                observed.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                observed.add(node.module.split(".", 1)[0])
    assert observed.isdisjoint(forbidden)
