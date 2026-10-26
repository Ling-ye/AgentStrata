from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from chatcopilot.botspec.inspection import declared_configuration
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.core.install_artifacts import private_directory, write_private
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.plugins import catalog as plugin_catalog
from chatcopilot.plugins.repository import InstallRepository
from chatcopilot.plugins.types import PluginDefinition
from console.backend.routes import architecture, bots, schedules, weixin
from console.control import discovery
from tests.application_fixture import write_application


@pytest.fixture
def plugin_console(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    entry = PluginDefinition("fixture", "fixture", "1.0.0", "must_not_be_imported", "3.12", "fixture")
    monkeypatch.setattr(plugin_catalog, "PLUGINS", {entry.id: entry})
    monkeypatch.setenv("AGENTSTRATA_PLUGINS_ROOT", str(plugins))
    monkeypatch.setattr(discovery, "repo_root", lambda: tmp_path / "repository")
    repo = InstallRepository(plugins, entry.id)
    private_directory(repo.root)
    private_directory(repo.root / "data")
    descriptions, recorders = [], []
    for name in ("first", "second"):
        spec = repo.root / f"config/{name}.yaml"
        env = repo.root / f"config/{name}.env"
        write_private(repo.root / "config/identity.md", "Fixture identity")
        write_private(repo.root / "config/style.md", "Respond clearly")
        write_application(spec, f"id: {name}\ndisplay_name: {name}\ngateway:\n"
            "  clients:\n    - id: fixture-owner\n      token_env: FIXTURE_TOKEN\n      role: owner\n"
            "prompts:\n  schema_version: 2\n  identity: identity.md\n  response_style: style.md\n"
            "agents:\n  runtime: langgraph\n")
        spec.chmod(0o600)
        spec.with_name(spec.stem + ".agent.yaml").chmod(0o600)
        state = GatewayStateStore(repo.root / f"data/{name}")
        generation = state.acquire_writer_generation()
        write_private(env, "CHATCOPILOT_GATEWAY_STATE_ROOT=" + str(state.root) + "\n")
        recorder = ObservationRecorder(state, generation, configuration=declared_configuration(spec, {}))
        state.create_session(generation=generation, session_id="shared-session",
            account=ChannelAccountRef("fixture", "account"), conversation=ConversationRef("p2p", "chat"))
        state.begin_run(generation=generation, session_id="shared-session", run_id="shared-run",
            input_fingerprint=hashlib.sha256(b"fixture").hexdigest())
        recorder.accepted("shared-run", name + " input", "owner", secrets=(), roots=())
        recorder.record("shared-run", {"kind": "ToolFinished", "layer": "capability", "name": "expression",
            "phase": "finish", "status": "succeeded"}, body={"status": "executed", "target": name})
        descriptions.append({"id": name, "display_name": name, "bot_spec": f"config/{name}.yaml",
                             "env_file": f"config/{name}.env"})
        recorders.append(recorder)
    repo.save({"version": entry.version, "status": "ready", "observable_agents": descriptions})
    app = FastAPI()
    # Fixed resource routes precede the generic /{instance_id}/{verb} route.
    for router in (architecture.router, schedules.router, weixin.router, bots.router):
        app.include_router(router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 45000))
    return SimpleNamespace(client=client, repo=repo, recorders=recorders)


def test_plugin_discovery_reads_existing_observations_configuration_and_scopes_bodies(plugin_console):
    h = plugin_console
    listing = h.client.get("/api/bots")
    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    assert [i["instance_id"] for i in listing.json()] == ["plugin:fixture:first", "plugin:fixture:second"]
    assert listing.json()[0]["origin"] == {"kind": "plugin", "plugin_id": "fixture", "version": "1.0.0", "agent_id": "first"}
    assert listing.json()[0]["unit"] == listing.json()[0]["wsl_home"] == ""
    assert discovery.discover_instances() == []
    first = "/api/bots/plugin:fixture:first"
    second = "/api/bots/plugin:fixture:second"
    before = [r.state_store.database_path.read_bytes() for r in h.recorders]
    history = h.client.get(first + "/gateway-observation")
    assert history.status_code == 200 and history.json()["total"] == 1
    detail = h.client.get(first + "/gateway-observation/runs/shared-run")
    assert detail.status_code == 200
    event = detail.json()["observations"][-1]
    suffix = "/gateway-observation/runs/shared-run/details/" + event["body_ref"]
    value = h.client.get(first + suffix)
    assert value.json()["payload"] == {"status": "executed", "target": "first"}
    assert h.client.get(second + suffix).status_code == 404
    assert h.client.get(first + "/gateway-observation/runs/shared-run/events?after=" + str(event["seq"])).json()["observations"] == []
    inspection = h.client.get(first + "/inspection?run_id=shared-run")
    assert inspection.status_code == 200 and inspection.json()["current"] and inspection.json()["execution"]
    service = next(e for e in inspection.json()["current"]["entities"] if e["id"] == "service:instance")
    assert not service["effective_config"].get("wsl_home")
    assert h.client.get(first + "/tools").status_code == 200
    assert h.client.get(first + "/inventory").status_code == 200
    assert h.client.get(first + "/gateway-observation/metrics").status_code == 200
    trace = h.client.get(first + "/gateway-observation/runs/shared-run/trace")
    assert trace.status_code == 200 and trace.json()["capture_state"] == "recording"
    for response in (history, detail, value, inspection, trace):
        assert response.headers["cache-control"] == "no-store"
    assert [r.state_store.database_path.read_bytes() for r in h.recorders] == before


def test_plugin_configuration_uses_declared_env_instead_of_repository_local_env(plugin_console):
    h = plugin_console
    write_private(h.repo.root / "config/local.env", "CHATCOPILOT_GATEWAY_STATE_ROOT=/wrong/state\n")
    response = h.client.get("/api/bots/plugin:fixture:first/inspection")
    assert response.status_code == 200 and response.json()["current"]
    gateway = next(e for e in response.json()["current"]["entities"] if e["id"] == "gateway:instance")
    assert gateway["effective_environment"]["CHATCOPILOT_GATEWAY_STATE_ROOT"] == str(h.repo.root / "data/first")


@pytest.mark.parametrize("method,suffix,payload", [
    ("POST", "/start", None), ("POST", "/stop", None), ("POST", "/restart", None),
    ("POST", "/sync", None), ("POST", "/update", None), ("POST", "/rebuild", None),
    ("POST", "/dump", None), ("POST", "/register", None), ("POST", "/setup-actions/fixture", None),
    ("POST", "/provision/env", {"SECRET": "must-not-be-written"}),
    ("PUT", "/tools", {"tools": {"packs": []}}),
    ("DELETE", "/tasks/fixture", None),
    ("POST", "/schedules", {"request_id": "fixture", "settings": {"name": "fixture", "instruction": "fixture", "group_id": "12345"}}),
    ("PUT", "/schedules/fixture", {"revision": 1, "settings": {"name": "fixture", "instruction": "fixture", "group_id": "12345"}}),
    ("DELETE", "/schedules/fixture?revision=1", None),
    ("POST", "/schedules/fixture/runs", {"revision": 1, "request_id": "fixture"}),
    ("POST", "/schedule-runs/fixture/cancel", None),
    ("POST", "/channels/weixin/login", None),
    ("POST", "/channels/weixin/login/fixture/verify", {"code": "123"}),
    ("DELETE", "/channels/weixin/login/fixture", None),
    ("GET", "/status", None), ("GET", "/logs/stream", None),
])
def test_plugin_instances_reject_managed_routes_before_side_effects(plugin_console, monkeypatch, method, suffix, payload):
    h = plugin_console

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only instance reached an operational side effect")

    monkeypatch.setattr(bots, "start_task", forbidden)
    monkeypatch.setattr(bots.operations, "control", forbidden)
    monkeypatch.setattr(bots.operations, "write_instance_env", forbidden)
    monkeypatch.setattr(schedules, "service_for", forbidden)
    monkeypatch.setattr(weixin, "_control", forbidden)
    response = h.client.request(method, "/api/bots/plugin:fixture:first" + suffix, json=payload)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "plugin_instance_read_only"
    assert response.headers["cache-control"] == "no-store"


def test_registered_agent_without_observation_store_is_unavailable_without_creation(plugin_console):
    h = plugin_console
    write_private(h.repo.root / "config/first.env", "CHATCOPILOT_GATEWAY_STATE_ROOT=" + str(h.repo.root / "not-created") + "\n")
    assert len(h.client.get("/api/bots").json()) == 2
    response = h.client.get("/api/bots/plugin:fixture:first/gateway-observation")
    assert response.status_code == 409
    assert not (h.repo.root / "not-created").exists()


def test_plugin_sse_reuses_cursor_and_private_body_projection(plugin_console):
    class Connection:
        headers = {"last-event-id": "0"}

        async def is_disconnected(self):
            return False

    async def observe():
        response = await architecture.gateway_run_stream("plugin:fixture:first", "shared-run", Connection(), after=0)
        assert response.headers["cache-control"] == "no-store"
        frame = await anext(response.body_iterator)
        assert "event: observation" in frame and '"run_id":"shared-run"' in frame
        cursor = int(frame.splitlines()[0].removeprefix("id: "))
        await response.body_iterator.aclose()
        from console.control.observation_stream import stream_page
        from console.backend.routes.common import get_observable_instance
        page = stream_page(get_observable_instance("plugin:fixture:first"), "shared-run", cursor)
        assert all(int(f.splitlines()[0].removeprefix("id: ")) > cursor for f in page["frames"])

    asyncio.run(observe())


def test_uninstalled_plugin_disappears_but_gateway_data_is_retained(plugin_console):
    h = plugin_console
    record = h.repo.read()
    h.repo.save({**record, "status": "uninstalled", "observable_agents": []})
    assert h.client.get("/api/bots").json() == []
    assert h.client.get("/api/bots/plugin:fixture:first/gateway-observation").status_code == 404
    assert h.recorders[0].store.database.is_file()
