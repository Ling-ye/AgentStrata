"""Periodic GC is opt-in and dispatches at most one ordinary task per slot."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.schedule_runtime import GovernanceScheduler
from chatcopilot.harness.governance_types import GovernanceOptions, GovernanceSchedule
from console.backend.routes.harness import router


@pytest.fixture
def scheduler(tmp_path):
    controller = SimpleNamespace(repository=tmp_path / "repo", default_model="fixture",
        active_governance_run=Mock(return_value=None),
        store=SimpleNamespace(root=tmp_path / "private", active_governance=Mock(return_value=None)),
        start_code_health=Mock(return_value={"run_id": "gc-example"}))
    systemctl = Mock(return_value=subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=active\n", ""))
    runtime = GovernanceScheduler(controller, unit_directory=tmp_path / "units", command=systemctl, clock=lambda: 86400)
    return runtime, controller, systemctl


def settings(enabled=True):
    return GovernanceSchedule(enabled, 24, GovernanceOptions("fixture", stop_condition={"mode": "findings", "count": 2}))


def test_default_disabled_never_installs_units_or_submits(scheduler):
    runtime, controller, systemctl = scheduler
    assert runtime.get()["enabled"] is False
    assert runtime.tick() == {"status": "disabled"}
    controller.start_code_health.assert_not_called()
    assert not runtime.units.exists()
    assert all("show" in call.args[0] for call in systemctl.call_args_list)


def test_enable_installs_only_trigger_and_same_slot_is_idempotent(scheduler):
    runtime, controller, systemctl = scheduler
    value = runtime.configure(settings())
    assert value["enabled"] and value["options"]["model"] == "fixture"
    service = (runtime.units / (runtime.unit + ".service")).read_text()
    timer = (runtime.units / (runtime.unit + ".timer")).read_text()
    assert "gc-tick" in service and "inspect unused helpers" not in service
    assert "OnUnitActiveSec=24h" in timer
    first = runtime.tick()
    again = GovernanceScheduler(controller, unit_directory=runtime.units, command=systemctl, clock=runtime.clock).tick()
    assert first["run_id"] == again["run_id"] and again["duplicate"]
    controller.start_code_health.assert_called_once()
    assert controller.start_code_health.call_args.kwargs["request_id"] == first["request_id"]


def test_pending_governance_delivery_skips_without_building_a_queue(scheduler):
    runtime, controller, _ = scheduler
    runtime.configure(settings())
    controller.store.active_governance.return_value = "repair-existing"
    assert runtime.tick()["status"] == "skipped"
    assert runtime.tick()["duplicate"]
    controller.start_code_health.assert_not_called()


def test_failed_activation_leaves_dispatch_disabled(scheduler):
    runtime, controller, systemctl = scheduler
    systemctl.return_value = subprocess.CompletedProcess([], 1, "", "unavailable")
    with pytest.raises(HarnessError):
        runtime.configure(settings())
    assert runtime.store.read()["enabled"] is False
    assert runtime.tick()["status"] == "disabled"
    controller.start_code_health.assert_not_called()


def test_disable_does_not_cancel_existing_task(scheduler):
    runtime, controller, systemctl = scheduler
    runtime.configure(settings())
    runtime.tick()
    runtime.configure(settings(False))
    assert runtime.tick()["status"] == "disabled"
    assert any("disable" in call.args[0] for call in systemctl.call_args_list)
    controller.start_code_health.assert_called_once()


def test_lost_dispatch_response_reuses_request_identity(scheduler):
    runtime, controller, _ = scheduler
    runtime.configure(settings())
    controller.start_code_health.side_effect = [RuntimeError("response lost"), {"run_id": "gc-existing"}]
    with pytest.raises(RuntimeError):
        runtime.tick()
    runtime.tick()
    calls = controller.start_code_health.call_args_list
    assert calls[0].kwargs["request_id"] == calls[1].kwargs["request_id"]


def test_console_schedule_and_gc_creation_use_local_public_entrypoints():
    app = FastAPI()
    app.include_router(router)
    controller = SimpleNamespace(start_code_health=Mock(return_value={"run_id": "gc-example"}),
        set_governance_schedule=Mock(return_value={"enabled": True}))
    app.state.harness = controller
    body = {"model": "fixture", "request_id": "gc", "stop_condition": {"mode": "findings", "count": 2}}
    schedule = settings().to_payload()
    with TestClient(app, client=("192.0.2.5", 41000)) as client:
        assert client.post("/api/harness/code-health/runs", json=body).status_code == 403
        assert client.put("/api/harness/code-health/schedule", json=schedule).status_code == 403
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert client.post("/api/harness/code-health/runs", json=body).status_code == 200
        assert controller.start_code_health.call_args.args[0].stop_condition == {"mode": "findings", "count": 2}
        assert client.put("/api/harness/code-health/schedule", json=schedule).status_code == 200
        assert controller.set_governance_schedule.call_args.args[0].options.stop_condition == body["stop_condition"]
        for fields in ({"single_issue": True}, {"repair_hint": "hint"}, {"feedback": {"expected_behavior": "replace rules"}},
                       {"timeout_seconds": 3600}, {"stop_condition": {"mode": "findings", "count": 1, "seconds": 3600}}):
            assert client.post("/api/harness/code-health/runs", json={**body, **fields}).status_code == 422
        assert client.post("/api/harness/tasks", json={"source_kind": "code_health", "model": "fixture", "request_id": "old"}).status_code == 422


def test_explicit_resave_replaces_old_schedule_without_migration(scheduler):
    runtime, _, _ = scheduler
    runtime.store.write({"enabled": False, "interval_hours": 24, "options": None, "repair_hint": "old"})
    with pytest.raises(ValueError, match="重新保存"):
        runtime.get()
    assert runtime.configure(settings())["options"]["stop_condition"] == {"mode": "findings", "count": 2}
    assert "repair_hint" not in runtime.store.read()


def test_schedule_model_preflight_failure_creates_no_run_and_can_retry(scheduler):
    runtime, controller, _ = scheduler
    runtime.configure(settings())
    controller.start_code_health.side_effect = HarnessError("model_unavailable", "worker model unavailable")
    with pytest.raises(HarnessError, match="worker model unavailable"):
        runtime.tick()
    assert runtime.store.read()["last_run"]["status"] == "failed"
    controller.start_code_health.side_effect = None
    assert runtime.tick()["status"] == "created"
    assert controller.start_code_health.call_count == 2
