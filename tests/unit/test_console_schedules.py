from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes import schedules
from chatcopilot.schedules.service import ScheduleService


@pytest.fixture(params=["isolated", "application"])
def api(tmp_path, monkeypatch, request):
    service = ScheduleService(tmp_path / "state")
    monkeypatch.setattr(schedules, "get_instance", lambda id: SimpleNamespace(instance_id=id))
    monkeypatch.setattr(schedules, "service_for", lambda inst: service)
    if request.param == "application":
        from console.backend import app as backend
        app = backend.app
        monkeypatch.setattr(app.state, "harness", object(), raising=False)
    else:
        app = FastAPI()
        app.include_router(schedules.router)
    return app, service


def body(**changes):
    return {"request_id": "create-fixture", "settings": {
        "name": "昨日推文", "instruction": "调查 @fixture 并附原文链接", "group_id": "30003", **changes}}


def test_api_create_preview_cancel_history_and_conflict(api):
    app, service = api
    prefix = "/api/bots/fixture"
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 45000)) as client:
        response = client.post(prefix + "/schedules", json=body())
        assert response.status_code == 200, response.text
        task = response.json()
        assert not task["settings"]["enabled"]
        assert len(client.get(prefix + "/schedules").json()["tasks"]) == 1
        run = client.post(prefix + f"/schedules/{task['id']}/runs", json={"revision": 1, "request_id": "run"}).json()
        assert run["preview"] is True
        assert run["status"] == "queued"
        assert client.post(prefix + f"/schedules/{task['id']}/runs", json={"revision": 1, "request_id": "run"}).json()["id"] == run["id"]
        assert client.get(prefix + "/schedule-runs").json()["runs"][0]["id"] == run["id"]
        assert "prompt" not in client.get(prefix + "/schedule-runs").json()["runs"][0]
        assert client.get(prefix + f"/schedule-runs/{run['id']}").json()["prompt"]
        assert client.post(prefix + f"/schedule-runs/{run['id']}/cancel").json()["status"] == "cancelled"
        assert client.put(prefix + f"/schedules/{task['id']}", json={"revision": 9, "settings": task["settings"]}).status_code == 409
        updated = client.put(prefix + f"/schedules/{task['id']}",
            json={"revision": 1, "settings": {**task["settings"], "name": "更新后的调查任务"}})
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert client.get(prefix + "/schedules").json()["tasks"][0]["settings"]["name"] == "更新后的调查任务"
        assert client.delete(prefix + f"/schedules/{task['id']}?revision=2").status_code == 200
        assert service.history()["runs"]
        assert client.get(prefix + "/schedule-runs/missing").status_code == 404


@pytest.mark.parametrize("host,base,origin", [
    ("192.0.2.8", "http://127.0.0.1", None),
    ("127.0.0.1", "http://127.0.0.1", "https://evil.example"),
    ("127.0.0.1", "http://evil.example", "http://evil.example"),
    ("127.0.0.1", "http://localhost", "null"),
])
def test_remote_csrf_and_dns_rebinding_writes_rejected_before_state(api, host, base, origin):
    app, service = api
    with TestClient(app, base_url=base, client=(host, 45000)) as client:
        response = client.post("/api/bots/fixture/schedules", json=body(), headers={"Origin": origin} if origin else {})
        assert response.status_code == 403
    assert not service.overview()["tasks"]


@pytest.mark.parametrize("changes", [{"group_id": "abc"}, {"timezone": "invalid"}, {"time": "99:00"}, {"webhook": "https://evil.example"}])
def test_api_invalid_definitions_have_no_side_effects(api, changes):
    app, service = api
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 45000)) as client:
        assert client.post("/api/bots/fixture/schedules", json=body(**changes)).status_code == 422
    assert not service.overview()["tasks"]


def test_instance_resolution_and_no_store(tmp_path, monkeypatch):
    from console.backend import app as backend
    from console.control.schedules import service_for
    from console.control.instances import BotInstance
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    spec = tmp_path / "bot.yaml"
    spec.write_text("gateway:\n  state_root_env: FIXTURE_STATE\n")
    env = tmp_path / "local.env"
    env.write_text(f"FIXTURE_STATE={state}\n")
    env.chmod(0o600)
    inst = BotInstance("fixture", str(spec), platform="qq", runtime_kind="gateway", env_file=str(env))
    service = service_for(inst)
    monkeypatch.setattr(schedules, "get_instance", lambda id: inst)
    monkeypatch.setattr(schedules, "service_for", lambda inst: service)
    monkeypatch.setattr(backend.app.state, "harness", object(), raising=False)
    with TestClient(backend.app) as client:
        response = client.get("/api/bots/fixture/schedules")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_complete_app_preserves_bot_control_routes(monkeypatch, verb):
    from console.backend import app as backend
    from console.backend.routes import bots
    inst = SimpleNamespace(instance_id="fixture")
    control = Mock(return_value={"ok": True})
    monkeypatch.setattr(backend.app.state, "harness", object(), raising=False)
    monkeypatch.setattr(bots, "get_instance", lambda id: inst)
    monkeypatch.setattr(bots.operations, "control", control)
    with TestClient(backend.app, base_url="http://localhost", client=("127.0.0.1", 45000)) as client:
        assert client.post(f"/api/bots/fixture/{verb}").json() == {"ok": True}
        response = client.post("/api/bots/fixture/unsupported-action")
        assert response.status_code == 404
    control.assert_called_once_with(inst, verb)
