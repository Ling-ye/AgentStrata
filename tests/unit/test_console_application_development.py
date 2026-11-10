"""Scoped prompt development cannot inherit another app's source or raw output."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from console.backend.routes.application_development import router
from console.control.application_development import Development
from console.control.applications import Applications


@pytest.fixture
def development(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    apps = Applications(root, tmp_path / "studio", discover=lambda: [])
    project = uuid4().hex
    apps.studio().create(project, application_type="qq_official")
    state = {"state": "prepared", "source_digest": "a" * 64, "definition_matches": True,
             "message": "ready"}
    task = {"task_id": "repair-" + "b" * 32, "status": "running",
            "source": {"bot_id": "qq-app-" + project}, "message": "PRIVATE-OUTPUT", "worktree": "/private/location"}
    host = Mock()
    host.settings = {}
    host.start_improvement.return_value = task
    host.get.return_value = task
    host.list.return_value = {"tasks": [task], "total": 1}
    manager = Mock()
    manager.source_status.return_value = state
    manager.source_controller.return_value = host
    manager.prepare_source.return_value = state
    manager.install_delivery.return_value = {**state, "state": "installed"}
    apps.manager = lambda: manager
    monkeypatch.setattr("console.control.application_development.worker_profile", lambda *a, **k: (
        SimpleNamespace(model="synthetic", reasoning_effort="", credential_root=str(tmp_path), profile_id="test"), {}))
    monkeypatch.setattr("chatcopilot.external_tools.codex_cli.auth_cli.status_lanes",
                        lambda *a: [SimpleNamespace(state="ready")])
    app = FastAPI()
    app.state.applications = apps
    app.include_router(router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        yield SimpleNamespace(app=app, client=client, apps=apps, project=project, manager=manager,
                              host=host, task=task, state=state,
                              url="/api/applications/studio:" + project + "/development")


def test_explicit_development_uses_frozen_project_and_prompt_only_scope(development):
    d = development
    request = {"id": str(uuid4()), "goal": "请给出更简洁的回复"}
    result = d.client.post(d.url + "/tasks", json=request)
    assert result.status_code == 200, result.text
    payload = d.host.start_improvement.call_args.args[0]
    assert payload.bot_id == "qq-app-" + d.project
    assert payload.allowed_paths == (f"bots/{payload.bot_id}/identity.md", f"bots/{payload.bot_id}/response.md")
    assert payload.suite_id == "agentstrata-agent-tasks-v1"
    d.manager._require_stopped.assert_called_once_with(d.project)
    assert d.manager.source_controller.call_args_list[-1].kwargs == {"start_eval": True}
    assert "PRIVATE-OUTPUT" not in result.text and "/private/location" not in result.text
    assert not result.json()["can_install"]
    # Caller-selected writable paths or provider credentials are not accepted.
    assert d.client.post(d.url + "/tasks", json={**request, "allowed_paths": ["src/"]}).status_code == 422


def test_request_identity_cannot_change_goal_project_or_baseline(development):
    d = development
    request = {"id": str(uuid4()), "goal": "brief"}
    assert d.client.post(d.url + "/tasks", json=request).status_code == 200
    assert d.client.post(d.url + "/tasks", json={**request, "goal": "different"}).status_code == 409
    second = uuid4().hex
    d.apps.studio().create(second, application_type="qq_official")
    assert d.client.post(d.url.replace(d.project, second) + "/tasks", json=request).status_code == 409
    d.state["source_digest"] = "c" * 64
    assert d.client.post(d.url + "/tasks", json=request).status_code == 409
    assert d.host.start_improvement.call_count == 1


def test_task_list_filters_before_pagination_and_rejects_cross_app(development):
    d = development
    result = d.client.get(d.url + "/tasks?page=2&bot_id=someone-else")
    assert result.status_code == 200
    d.host.list.assert_called_once_with(page=2, limit=20, bot_id="qq-app-" + d.project)
    d.task["source"]["bot_id"] = "other"
    assert d.client.get(d.url + "/tasks").status_code == 404
    assert d.client.post(d.url + "/tasks/" + d.task["task_id"] + "/install").status_code == 404
    d.manager.install_delivery.assert_not_called()


def test_unprepared_listing_never_creates_service_and_mutations_are_local(development):
    d = development
    d.state["state"] = "unprepared"
    assert d.client.get(d.url + "/tasks").json()["tasks"] == []
    d.manager.source_controller.assert_not_called()
    assert d.client.post(d.url + "/prepare", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    with TestClient(d.app, base_url="http://localhost", client=("192.0.2.1", 1234)) as remote:
        assert remote.post(d.url + "/prepare").status_code == 403
    d.manager.prepare_source.assert_not_called()


def test_install_is_exact_scoped_explicit_and_never_starts_application(development):
    d = development
    task_id = d.task["task_id"]
    assert d.client.post(d.url + "/tasks/" + task_id + "/install").status_code == 200
    d.manager.install_delivery.assert_called_once_with(d.project, task_id)
    d.manager.start.assert_not_called()
    d.manager.install_delivery.side_effect = ValueError("PRIVATE-OUTPUT /private/path")
    response = d.client.post(d.url + "/tasks/" + task_id + "/install")
    assert response.status_code == 409 and "PRIVATE-OUTPUT" not in response.text
    assert d.client.get(d.url + "/tasks/not-a-task").status_code == 422


def test_public_task_view_never_echoes_unknown_status_or_worker_error():
    result = Development.view({"task_id": "repair-" + "a" * 32, "status": "PRIVATE-OUTPUT",
                               "source": {"bot_id": "qq-app-p"}, "error": "PRIVATE-OUTPUT"}, "p")
    assert result["status"] == "processing"
    assert "PRIVATE-OUTPUT" not in json.dumps(result)
