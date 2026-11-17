"""The installed Studio entry protects every Console API with launcher auth."""
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from studio.console_server import application
from console.backend.routes import development_setup


def client(tmp_path):
    app = application(tmp_path / "studio", Path(__file__).resolve().parents[2], "fixture-launch-token")
    app.state.applications._discover = lambda: []
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))


def test_launcher_cookie_guards_all_console_routes(tmp_path):
    c = client(tmp_path)
    for path in ("/api/applications", "/api/llm/config", "/api/development/setup", "/api/overview"):
        assert c.get(path).status_code == 401
    assert c.post("/api/login", json={"token": "wrong"}).status_code == 403
    response = c.post("/api/login", json={"token": "fixture-launch-token"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert c.get("/api/applications").json() == {"applications": []}
    assert c.get("/api/llm/config").status_code == 200
    assert c.get("/api/llm/config").headers["cache-control"] == "no-store"


def test_cookie_cannot_cross_new_server_instance(tmp_path):
    first, second = client(tmp_path), client(tmp_path)
    first.post("/api/login", json={"token": "fixture-launch-token"})
    second.cookies.update(first.cookies)
    assert second.get("/api/applications").status_code == 401


def test_cross_origin_and_nonlocal_rejected(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/login", json={"token": "fixture-launch-token"},
                  headers={"Origin": "https://example.invalid"}).status_code == 403
    assert c.get("/api/llm/config", headers={"Host": "example.invalid"}).status_code == 403
    remote = TestClient(c.app, base_url="http://127.0.0.1", client=("192.0.2.1", 12345))
    assert remote.post("/api/login", json={"token": "fixture-launch-token"}).status_code == 403


def test_models_and_application_tasks_use_same_installed_store(tmp_path):
    c = client(tmp_path)
    assert c.app.state.llm.store.path == tmp_path / "studio/models.json"
    assert c.app.state.applications.model_path == c.app.state.llm.store.path
    assert c.app.state.applications.state_dir == tmp_path / "studio"


def test_missing_development_binding_does_not_block_api_only_application(tmp_path):
    c = client(tmp_path)
    c.post("/api/login", json={"token": "fixture-launch-token"})
    response = c.get("/api/development/setup")
    assert response.status_code == 200
    assert response.json()["bindings"] == {"chat": False, "harness": False, "evaluation.judge": False}
    assert not response.json()["login_available"]
    assert c.post("/api/development/setup/login/worker").status_code == 400
    assert c.post("/api/development/setup/login/invalid").status_code == 400


def test_authorization_uses_resolved_server_side_authority(tmp_path, monkeypatch):
    c = client(tmp_path)
    c.post("/api/login", json={"token": "fixture-launch-token"})
    calls = []
    cfg = SimpleNamespace(credential_root="/private/authority", codex_bin="/installed/codex")
    monkeypatch.setattr(development_setup, "account", lambda manager, lane: cfg)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-test")
    monkeypatch.setattr(development_setup.subprocess, "run", lambda args, **kwargs: calls.append(args))
    from studio import delivery
    monkeypatch.setattr(delivery, "windows_path", str)
    assert c.post("/api/development/setup/login/worker").status_code == 200
    assert calls[0][-4:] == ["-AuthRoot", "/private/authority", "-CodexBin", "/installed/codex"]



def test_invalid_login_does_not_echo_input(tmp_path):
    c = client(tmp_path)
    response = c.post("/api/login", json={"token": {"secret": "fixture-private-value"}})
    assert response.status_code == 422
    assert "fixture-private-value" not in response.text


def test_guide_inherits_authority_without_overriding_shared_models(tmp_path, monkeypatch):
    from studio.setup import Guide
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", "/explicit/authority")
    monkeypatch.setenv("CHATCOPILOT_CODEX_BIN", "/explicit/codex")
    guide = Guide(tmp_path, tmp_path, model_path=tmp_path / "shared.json")
    assert guide.model_environment["CHATCOPILOT_CODEX_BOT_HOME"] == "/explicit/authority"
    assert guide.model_environment["CHATCOPILOT_CODEX_BIN"] == "/explicit/codex"
    assert guide.model_environment["AGENTSTRATA_LLM_CONFIG"] == str(tmp_path / "shared.json")



def test_installed_model_catalog_preserves_explicit_connection_environment(tmp_path, monkeypatch):
    from studio import console_server
    original = console_server.ModelControl
    seen = []
    def model_control(framework, *, environment):
        seen.append(environment)
        return original(framework, environment=environment)
    monkeypatch.setattr(console_server, "ModelControl", model_control)
    monkeypatch.setenv("FIXTURE_MODEL_KEY", "synthetic-fixture")
    client(tmp_path)
    assert seen[0]["FIXTURE_MODEL_KEY"] == "synthetic-fixture"
    assert seen[0]["AGENTSTRATA_LLM_CONFIG"] == str(tmp_path / "studio/models.json")
