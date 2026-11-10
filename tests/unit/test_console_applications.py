"""Application workbench source isolation and private control API regressions."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.control.applications import Applications
from console.control.instances import BotInstance
from console.backend.routes.applications import router


@pytest.fixture
def applications(tmp_path):
    return Applications(Path(__file__).resolve().parents[2], tmp_path / "studio",
                        model_path=tmp_path / "shared-models.json", discover=lambda: [])


def create(applications):
    return applications.create(uuid4().hex, "qq_official")


def test_read_does_not_create_state(applications):
    assert applications.catalog() == []
    assert not applications.state_dir.exists()


def test_sources_keep_stable_ids_and_controls(applications, tmp_path):
    spec = str(tmp_path / "repo.yaml")
    repo = BotInstance("same", spec, display_name="renamable")
    plugin = BotInstance("plugin:a", str(tmp_path / "plugin.yaml"), origin={"kind": "plugin", "plugin_id": "open-llm-vtuber"})
    applications._discover = lambda: [repo, repo, plugin]
    rows = applications.catalog()
    assert [r["id"] for r in rows] == ["repo:same", "plugin:plugin:a"]
    assert rows[0]["capabilities"]["control"]
    assert not rows[1]["capabilities"]["control"]
    assert rows[1]["type"] == "companion"
    assert not any(r["capabilities"]["chat"] for r in rows)
    repo.display_name = "new name"
    assert applications.catalog()[0]["id"] == "repo:same"


def test_studio_source_cannot_acquire_repository_control(applications):
    row = create(applications)
    generated = applications.state_dir / "applications" / row["project_id"] / "bot.yaml"
    applications._discover = lambda: [BotInstance("alias", str(generated))]
    rows = applications.catalog()
    assert len(rows) == 1 and rows[0]["source"] == "studio"


def test_configuration_never_returns_secret(applications):
    row = create(applications)
    secret = "fixture-secret-value"
    result = applications.configure(row["id"], {"name": "test", "instructions": "answer clearly",
                                               "app_id": "123", "app_secret": secret})
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(applications.detail(row["id"]))
    assert applications.manager().model_path == applications.model_path
    assert applications.require(row["id"])["name"] == "test"


def test_message_identity_and_source_isolation(applications):
    a, b = create(applications), create(applications)
    message = uuid4().hex
    applications.submit(a["id"], message, "new purpose")
    applications.submit(a["id"], message, "new purpose")
    assert len(applications.messages(a["id"])) == 1
    assert applications.messages(b["id"]) == []
    assert message not in applications.studio().pending()  # Never picked up by development polling.
    with pytest.raises(ValueError):
        applications.submit(b["id"], message, "new purpose")
    with pytest.raises(ValueError):
        applications.submit(a["id"], message, "different")
    with pytest.raises(KeyError):
        applications.submit("studio:" + uuid4().hex, uuid4().hex, "text")


def test_only_one_pending_operation_and_existing_development_guard(applications):
    import sqlite3
    a = create(applications)
    applications.submit(a["id"], uuid4().hex, "first")
    with pytest.raises(sqlite3.IntegrityError):
        applications.submit(a["id"], uuid4().hex, "second")
    with pytest.raises(sqlite3.IntegrityError):
        applications.studio().submit(a["project_id"], uuid4().hex, "legacy")


def test_proposal_requires_correct_app_and_explicit_apply(applications, monkeypatch):
    from studio import runtime
    from studio.setup import Guide
    a, b = create(applications), create(applications)
    applications.configure(a["id"], {"name": "original", "instructions": "old", "app_id": "123", "app_secret": "fixture-secret"})
    message = uuid4().hex
    applications.submit(a["id"], message, "make concise")
    contexts = []
    async def discuss(home, project, request, text):
        contexts.append(json.loads(text))
        return {"action": "configure", "name": "changed", "instructions": "brief", "reply": "confirm"}
    monkeypatch.setattr(runtime, "discuss", discuss)
    monkeypatch.setattr(Guide, "start", lambda self: None)
    applications.process_message(a["id"], message)
    assert applications.configuration(a["id"])["name"] == "original"
    assert "fixture-secret" not in json.dumps(contexts)
    assert "app_id" not in json.dumps(contexts)
    with pytest.raises(ValueError):
        applications.apply(b["id"], message)
    applications.apply(a["id"], message)
    assert applications.configuration(a["id"])["name"] == "changed"
    assert applications.configuration(a["id"])["app_id"] == "123"
    assert applications.messages(a["id"])[0]["state"] == "applied"
    applications.configure(a["id"], {"name": "manual B"})
    applications.apply(a["id"], message)
    assert applications.configuration(a["id"])["name"] == "manual B"

    # A request's input is frozen at submission; subsequent writes invalidate its proposal.
    key = uuid4().hex
    applications.submit(a["id"], key, "next")
    applications.configure(a["id"], {"name": "manual C"})
    applications.process_message(a["id"], key)
    assert contexts[-1]["saved_definition"]["name"] == "manual B"
    with pytest.raises(ValueError, match="配置已变更"):
        applications.apply(a["id"], key)
    assert applications.configuration(a["id"])["name"] == "manual C"

    # Manifest receipt also protects a retry after a crash before the message state update.
    applications.studio().update(message, "configure", "retry")
    manager = applications.manager()
    result = manager.configure(a["project_id"], {"name": "stale"}, expected_version="old", proposal_id=message)
    assert result["name"] == "manual C"


def test_management_cannot_execute_development(applications, monkeypatch):
    from studio import runtime
    from studio.setup import Guide
    a = create(applications)
    message = uuid4().hex
    applications.submit(a["id"], message, "write code")
    async def discuss(*args):
        return {"action": "develop", "reply": "go"}
    monkeypatch.setattr(runtime, "discuss", discuss)
    monkeypatch.setattr(runtime, "start_task", lambda *a, **k: pytest.fail("must not develop"))
    monkeypatch.setattr(Guide, "start", lambda self: None)
    applications.process_message(a["id"], message)
    assert applications.messages(a["id"])[0]["state"] == "discuss"


def test_restart_only_resumes_owned_configuration_requests(applications, monkeypatch):
    a = create(applications)
    key = uuid4().hex
    applications.submit(a["id"], key, "test")
    restored = Applications(applications.root, applications.state_dir, discover=lambda: [])
    calls = []
    def process(ident, message):
        calls.append((ident, message))
        restored.studio().update(message, "discuss", "done")
    monkeypatch.setattr(restored, "process_message", process)
    async def run():
        task = asyncio.create_task(restored.resume())
        while not calls:
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert calls == [(a["id"], key)]


def client_for(applications, host="127.0.0.1"):
    app = FastAPI()
    app.state.applications = applications
    app.include_router(router)
    return TestClient(app, base_url="http://localhost", client=(host, 12345))


def test_api_rejects_remote_cross_origin_and_secret_echo(applications):
    with client_for(applications, "192.0.2.1") as remote:
        assert remote.get("/api/applications").status_code == 403
    with client_for(applications) as client:
        assert client.get("/api/applications", headers={"origin": "https://evil.invalid"}).status_code == 403
        result = client.post("/api/applications", json={"id": str(uuid4()), "type": "qq_official"})
        assert result.status_code == 200
        ident = result.json()["id"]
        response = client.post(f"/api/applications/{ident}/configuration",
                               json={"name": "test", "app_secret": {"nested": "DO-NOT-ECHO"}})
        assert response.status_code == 422 and "DO-NOT-ECHO" not in response.text


def test_unsupported_and_unknown_actions_cannot_reach_runtime(applications, monkeypatch):
    from console.control import operations
    applications._discover = lambda: [BotInstance("p", "/tmp/plugin.yaml", origin={"kind": "plugin"})]
    monkeypatch.setattr(operations, "control", lambda *a: pytest.fail("unexpected control"))
    with client_for(applications) as client:
        assert client.post("/api/applications/plugin:p/start").status_code == 409
        assert client.post("/api/applications/missing/start").status_code == 404
        assert client.post("/api/applications", json={"id": str(uuid4()), "type": "desktop"}).status_code == 400


def test_gateway_health_matches_host_credential_binding(applications, monkeypatch):
    from studio.applications import ApplicationManager
    from chatcopilot.core.install_artifacts import write_private
    from chatcopilot.protocols import gateway_client
    key = uuid4().hex
    manager = applications.manager()
    root = manager._root(key)
    token = "x" * 48
    write_private(root / "runtime.env", 'CHATCOPILOT_GATEWAY_PORT="1234"\nCHATCOPILOT_GATEWAY_TOKEN="' + token + '"\n')
    class Client:
        def __init__(self, config):
            assert config.scopes == ("gateway.read", "chat.write", "chat.abort")
            assert config.client_id == "acp-edge" and config.client_mode == "acp"
        async def connect(self): pass
        async def close(self): pass
        async def request(self, method, params):
            assert method == "channels.list"
            return SimpleNamespace(channels=[SimpleNamespace(account=SimpleNamespace(channel="qq_official", account_id="123"),
                                                            state="connected", error_code=None)])
    monkeypatch.setattr(gateway_client, "GatewayWebSocketClient", Client)
    assert asyncio.run(ApplicationManager._query_channel(manager, root, "123")) == ("connected", None)



def test_concurrent_proposals_use_single_configuration_version(applications, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from studio.applications import ApplicationManager
    monkeypatch.setattr(ApplicationManager, "_active", lambda self, project: "inactive")
    row = create(applications)
    applications.configure(row["id"], {"name": "before"})
    manager = applications.manager()
    _, baseline = manager.definition_snapshot(row["project_id"])
    def apply(name):
        try:
            manager.configure(row["project_id"], {"name": name}, expected_version=baseline, proposal_id=name)
            return "applied"
        except ValueError:
            return "stale"
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(apply, ["first", "second"])) == ["applied", "stale"]


def test_receipt_retry_repairs_failed_materialization(applications, monkeypatch):
    from studio.applications import ApplicationManager
    monkeypatch.setattr(ApplicationManager, "_active", lambda self, project: "inactive")
    row = create(applications)
    manager = applications.manager()
    _, baseline = manager.definition_snapshot(row["project_id"])
    original = manager._materialize
    def fail(*args):
        raise OSError("injected")
    monkeypatch.setattr(manager, "_materialize", fail)
    with pytest.raises(OSError):
        manager.configure(row["project_id"], {"name": "new", "instructions": "new identity"},
                          expected_version=baseline, proposal_id="receipt")
    monkeypatch.setattr(manager, "_materialize", original)
    result = manager.configure(row["project_id"], {"name": "stale"}, expected_version=baseline, proposal_id="receipt")
    assert result["name"] == "new"
    assert "new identity" in (manager._root(row["project_id"]) / "identity.md").read_text()
