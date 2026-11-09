"""Standard applications do not enter the desktop build pipeline."""
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from studio import runtime
from studio.server import application
from studio.workspace import Studio

ROOT = Path(__file__).resolve().parents[2]


def test_configuration_proposal_does_not_start_worker(tmp_path, monkeypatch):
    studio = Studio(tmp_path, ROOT)
    project, message = uuid4().hex, uuid4().hex
    studio.create(project)
    studio.submit(project, message, "创建 QQ 官方机器人")
    async def discuss(*args):
        return {"action":"configure", "name":"项目助手", "instructions":"回答项目问题", "reply":"请确认"}
    monkeypatch.setattr(runtime, "discuss", discuss)
    monkeypatch.setattr(runtime, "start_task", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker must not run")))
    studio.step(message)
    assert studio.projects()[0]["application_type"] == "qq_official"
    row = studio.conversation(project)[0]
    assert row["state"] == "configure"
    assert row["proposal"] == {"name":"项目助手", "instructions":"回答项目问题"}
    assert not studio.pending()


def test_standard_type_is_idempotent_and_isolated(tmp_path):
    studio = Studio(tmp_path, ROOT)
    key = uuid4().hex
    assert studio.create(key, application_type="qq_official") == key
    assert studio.create(key, application_type="qq_official") == key
    import pytest
    with pytest.raises(ValueError):
        studio.create(key)


def test_application_routes_require_project_and_explicit_actions(tmp_path, monkeypatch):
    from studio import applications
    calls=[]
    class FakeManager:
        def __init__(self, *args): pass
        def get(self, key): return {"state":"not_created"}
        def configure(self, key, value):
            calls.append((key,value))
            return {"state":"configured"}
        def check(self, key): return {"state":"configured"}
        def start(self, key):
            calls.append((key,"start"))
            return {"state":"running_unverified"}
        def stop(self, key): return {"state":"stopped"}
    monkeypatch.setattr(applications, "ApplicationManager", FakeManager)
    app=application(tmp_path,ROOT,"login",run_worker=False)
    with TestClient(app,base_url="http://localhost") as client:
        project=uuid4().hex
        path=f"/api/projects/{project}/application"
        assert client.post(path+"/start",json={}).status_code == 401
        client.post("/api/login",json={"token":"login"})
        assert client.get(path).status_code == 404
        client.post("/api/projects",json={"id":project,"application_type":"qq_official"})
        assert client.get(path).json()["state"] == "not_created"
        assert not calls
        response=client.post(path+"/configure",json={"name":"QQ", "instructions":"help", "app_id":"123", "app_secret":"synthetic"})
        assert response.status_code == 200
        assert "synthetic" not in response.text
        secret = "private-test-" * 100
        rejected=client.post(path+"/configure",json={"app_secret":secret})
        assert rejected.status_code == 422
        assert secret not in rejected.text
        assert len(calls)==1
        assert client.post(path+"/start",json={}).status_code == 200
        assert calls[-1] == (project,"start")
        assert client.post(path+"/configure",json={"name":"QQ", "shell":"oops"}).status_code == 422


def test_api_setup_does_not_require_worker(tmp_path, monkeypatch):
    from studio.setup import Guide
    monkeypatch.setattr(Guide,"start",lambda self:None)
    monkeypatch.setattr(Guide,"stop",lambda self:None)
    with TestClient(application(tmp_path,ROOT,"login",run_worker=False),base_url="http://localhost") as client:
        client.post("/api/login",json={"token":"login"})
        response=client.post("/api/setup",json={"model":"test-model","endpoint":"https://example.com/v1","key":"synthetic-api"})
        assert response.status_code == 200,response.text
    data=json.loads((tmp_path/"models.json").read_text())
    assert "worker" not in data["profiles"]
    assert "harness" not in data["bindings"]
