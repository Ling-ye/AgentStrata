"""The application history BFF resolves scope, never accepts caller-selected bot IDs."""
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from console.backend.routes import application_records as routes


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    applications = Mock()
    applications.require.side_effect = lambda ident: {"source": "repository", "instance_id": ident.removeprefix("repo:")}
    app.state.applications = applications
    evaluations, harness = Mock(), Mock()
    evaluations.list.return_value = []
    harness.list.return_value = {"tasks": [], "total": 0, "page": 1, "limit": 20}
    monkeypatch.setattr(routes, "get_evaluation_client", lambda _: evaluations)
    monkeypatch.setattr(routes, "_controller", lambda _: harness)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as http:
        yield SimpleNamespace(http=http, applications=applications, evaluations=evaluations, harness=harness)


def test_history_uses_resolved_identity_and_explicit_pagination(client):
    assert client.http.get("/api/applications/repo:alpha/evaluations?bot_id=beta&offset=20&limit=10").status_code == 200
    client.evaluations.list.assert_called_once_with(bot_id="alpha", offset=20, limit=10)
    assert client.http.get("/api/applications/repo:beta/harness?page=2&limit=5&bot_id=alpha").status_code == 200
    client.harness.list.assert_called_once_with(bot_id="beta", page=2, limit=5)


def test_unsupported_source_never_falls_back_to_global_history(client):
    client.applications.require.side_effect = None
    client.applications.require.return_value = {"source": "studio", "project_id": "example"}
    for resource in ("evaluations", "harness"):
        assert client.http.get("/api/applications/studio:example/" + resource).status_code == 409
    client.evaluations.list.assert_not_called()
    client.harness.list.assert_not_called()


def test_missing_application_and_cross_origin_are_rejected(client):
    client.applications.require.side_effect = KeyError("missing")
    assert client.http.get("/api/applications/repo:missing/harness").status_code == 404
    assert client.http.get("/api/applications/repo:alpha/harness", headers={"Origin": "https://example.com"}).status_code == 403
    client.harness.list.assert_not_called()


def test_desktop_download_uses_exact_project_and_verified_artifact(client, tmp_path):
    message = "a" * 32
    client.applications.require.side_effect = None
    client.applications.require.return_value = {"source": "studio", "type": "desktop", "project_id": "b" * 32}
    artifact = tmp_path / "application.exe"
    artifact.write_bytes(b"verified fixture")
    resolve = client.applications.studio.return_value.artifact
    resolve.return_value = artifact
    response = client.http.get("/api/applications/studio:" + "b" * 32 + "/builds/" + message)
    assert response.status_code == 200 and response.content == b"verified fixture"
    resolve.assert_called_once_with("b" * 32, message)
    resolve.side_effect = KeyError("belongs to another application")
    assert client.http.get("/api/applications/studio:" + "b" * 32 + "/builds/" + message).status_code == 404
