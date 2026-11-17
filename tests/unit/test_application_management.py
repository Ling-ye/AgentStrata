"""Real private stores and authenticated RPC; no model, channel or installer calls."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.botspec.runtime import load_runtime_context
from chatcopilot.contracts.application_management import ApplicationReadParams, ApplicationWriteParams
from chatcopilot.contracts.gateway_protocol import RequestFrame
from chatcopilot.contracts.gateway_rpc import ChatSendParams, TextRpcSegment
from chatcopilot.core.application_settings import ApplicationSettings, SettingsConflict
from chatcopilot.core.config import ChatConfig
from chatcopilot.core.model_settings import ModelSettingsStore
from chatcopilot.gateway.dispatcher import GatewayApplicationDispatcher
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.gateway.protocol import GatewayCredentialBinding, StaticGatewayCredentialAuthority, GatewayProtocolError
from chatcopilot.gateway.rpc_validation import parse_request_params, serialize_request_params
from chatcopilot.gateway.server import GatewayWebSocketServer, GatewayServerConfig, GatewayClientContext, GatewayDispatchError
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.host.application_management import ApplicationManagement
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient, GatewayClientError
from console.control.applications import Applications
from console.control.instances import BotInstance
from console.backend.routes import application_management  # noqa: F401
from console.backend.routes.applications import router


@pytest.fixture
def management(tmp_path):
    runtime = load_runtime_context(Path(__file__).resolve().parents[2] / "bots/live2d-desktop/bot.yaml")
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    models = ModelSettingsStore(tmp_path / "models.json")
    document = {"connections": {"api": {"kind": "openai_compatible", "base_url": "https://fixture.invalid/v1",
                "auth": {"mode": "api_key", "key_env": "FIXTURE_KEY"}}},
                "profiles": {"one": {"connection": "api", "model": "first"}, "two": {"connection": "api", "model": "second"}},
                "bindings": {"chat": "one"}}
    models.save(document, revision=models.view()["revision"])
    return ApplicationManagement(runtime, root, {"AGENTSTRATA_LLM_CONFIG": str(models.path)})


def test_settings_are_instance_owned_and_do_not_rewrite_source_or_models(management):
    model_file = Path(management.environment["AGENTSTRATA_LLM_CONFIG"])
    before = model_file.read_bytes()
    prompt = management.runtime.prompt_profile
    current = management.settings()
    result = management.write(ApplicationWriteParams("prompts", current["prompts"]["revision"],
                              {"identity": "中文人设🙂", "response_style": "先给结论"}))
    assert result.revision != current["prompts"]["revision"]
    updated = management.store.apply_prompts(management.runtime)
    assert updated.prompt_profile.identity == "中文人设🙂"
    assert updated.prompt_profile.role_styles == prompt.role_styles
    assert management.runtime.prompt_profile == prompt
    management.write(ApplicationWriteParams("model", current["model"]["revision"], {"profile": "two"}))
    config = ChatConfig()
    config.model_settings = ModelSettingsStore(model_file).read()
    management.store.apply_model(config, management.environment)
    assert config.llm.profile_id == "two"
    assert model_file.read_bytes() == before
    other = ApplicationSettings(management.store.root, "another-application")
    with pytest.raises(ValueError, match="当前实例"):
        other.read("prompts", {})


def test_stale_forms_and_changed_model_catalog_do_not_overwrite(management):
    current = management.settings()
    values = {"identity": "new", "response_style": "brief"}
    management.write(ApplicationWriteParams("prompts", current["prompts"]["revision"], values))
    with pytest.raises(SettingsConflict):
        management.write(ApplicationWriteParams("prompts", current["prompts"]["revision"], values))
    models = ModelSettingsStore(Path(management.environment["AGENTSTRATA_LLM_CONFIG"]))
    data = models.view()
    expected = data.pop("revision")
    data["profiles"]["two"]["model"] = "changed-externally"
    models.save(data, revision=expected)
    with pytest.raises(SettingsConflict):
        management.write(ApplicationWriteParams("model", current["model"]["revision"], {"profile": "two"}))
    assert not (management.store.root / "user-model.json").exists()


def test_saved_and_loaded_revisions_are_distinct(management):
    store = ObservationStore(management.store.root, writable=True)
    config_id = store.put_configuration({"application_settings": management.loaded_revisions()})
    store.set_meta("loaded_configuration", {"config_id": config_id, "generation": 1, "observed_at": time.time()})
    assert management.settings()["prompts"]["status"] == "applied"
    current = management.settings()
    management.write(ApplicationWriteParams("prompts", current["prompts"]["revision"], {"identity": "new", "response_style": "brief"}))
    assert management.settings()["prompts"]["status"] == "pending"
    store.set_meta("runtime_status", {"state": "stopped", "generation": 1, "observed_at": time.time()})
    assert management.settings()["prompts"]["status"] == "unknown"


def test_large_unicode_observation_preserves_all_bytes(management, monkeypatch):
    value = {"payload": "测试🙂" * 12000}
    monkeypatch.setattr(management, "project", lambda *_: value)
    content = ""
    digest = ""
    while True:
        chunk = management.read(ApplicationReadParams("body", {"run_id": "run", "body_id": "body"}, len(content), digest))
        assert len(chunk.content) <= 16000 and chunk.content.isascii()
        digest = chunk.digest
        content += chunk.content
        if len(content) == chunk.total:
            break
    assert hashlib.sha256(content.encode()).hexdigest() == chunk.digest
    assert json.loads(content) == value


@pytest.mark.parametrize("params", [
    {"operation": "shell"}, {"operation": "history", "arguments": {"path": "/private"}},
    {"operation": "events", "arguments": {"run_id": "run", "limit": True}},
    {"operation": "body", "arguments": {"run_id": "run"}},
    {"operation": "settings", "arguments": ["invalid"]},
])
def test_query_contract_rejects_invalid_parameters(params):
    with pytest.raises(GatewayProtocolError):
        parse_request_params("application.read", params)


def test_prompt_settings_have_typed_round_trip_and_no_permission_fields():
    params = ApplicationWriteParams("prompts", "a" * 64, {"identity": "a", "response_style": "b"})
    assert parse_request_params("application.settings", serialize_request_params("application.settings", params)) == params
    with pytest.raises(GatewayProtocolError):
        serialize_request_params("application.settings", replace(params, values={"owner": "someone"}))


def test_management_scope_cannot_send_chat_or_mutate_with_read_only_scope(management, tmp_path):
    async def exercise():
        state = GatewayStateStore(tmp_path / "gateway")
        generation = state.acquire_writer_generation()
        dispatcher = GatewayApplicationDispatcher(state_store=state, sessions=SimpleNamespace(generation=generation, assert_current_generation=lambda: None),
            events=None, coordinator=None, channel_runtime=None, event_visibility=None, generation=generation,
            management=management)
        scopes = ("application.read", "application.settings")
        binding = GatewayCredentialBinding("t" * 48, "studio-manager", "management", scopes)
        server = GatewayWebSocketServer(config=GatewayServerConfig(port=0), dispatcher=dispatcher,
            credential_authority=StaticGatewayCredentialAuthority((binding,)), event_visibility_policy=SimpleNamespace(can_view=lambda **_: False),
            state_store=state, server_generation=generation)
        await server.start()
        client = GatewayWebSocketClient(GatewayClientConfig(server.url, binding.token, client_id="studio-manager", client_mode="management", scopes=scopes, capabilities=()))
        try:
            await client.connect()
            result = await client.request("application.read", ApplicationReadParams("identity"))
            assert json.loads(result.content)["bot_id"] == management.runtime.bot_id
            with pytest.raises(GatewayClientError):
                await client.request("chat.send", ChatSendParams("session", (TextRpcSegment("never sent"),), "key"), idempotency_key="key")
            current = management.settings()["prompts"]
            params = ApplicationWriteParams("prompts", current["revision"], {"identity": "changed", "response_style": "brief"})
            first = await client.request("application.settings", params, idempotency_key="same-save")
            again = await client.request("application.settings", params, idempotency_key="same-save")
            assert first == again
            readonly = GatewayClientContext("reader", "test", "management", 1, ("application.read",), ())
            with pytest.raises(GatewayDispatchError):
                await dispatcher.dispatch(RequestFrame("deny", "application.settings", serialize_request_params("application.settings", params), "deny"), client=readonly)
        finally:
            await client.close()
            await server.stop()
    asyncio.run(exercise())


def test_workbench_settings_and_observations_are_application_bound(management, tmp_path, monkeypatch):
    from console.control.application_management import Management
    app = FastAPI()
    instance = BotInstance(management.runtime.instance_id, str(management.runtime.source_path), runtime_kind="gateway")
    apps = Applications(tmp_path, tmp_path / "studio", discover=lambda: [instance])
    app.state.applications = apps
    app.include_router(router)
    monkeypatch.setattr(Management, "local", lambda self, ident: management if self.apps.require(ident) else None)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1))
    ident = "repo:" + instance.instance_id
    response = client.get(f"/api/applications/{ident}/settings")
    assert response.status_code == 200
    assert "api_key" not in response.text and "env_file" not in response.text
    assert client.get("/api/applications/repo:missing/settings").status_code == 404
    assert client.get(f"/api/applications/{ident}/settings", headers={"Origin": "http://evil.invalid"}).status_code == 403
    assert client.get(f"/api/applications/{ident}/observations/gateway-observation?path=/etc/passwd").status_code == 400
    saved = response.json()["prompts"]
    body = {"revision": saved["revision"], "values": {"identity": "new", "response_style": "short"}}
    assert client.post(f"/api/applications/{ident}/settings/prompts", json=body).status_code == 200
    assert client.post(f"/api/applications/{ident}/settings/prompts", json=body).status_code == 409


def test_observation_query_accepts_fractional_timestamps_but_not_nonfinite_values():
    params = {"operation": "history", "arguments": {"since": 1794830200.125, "page": 1}}
    parsed = parse_request_params("application.read", params)
    assert serialize_request_params("application.read", parsed)["arguments"] == params["arguments"]
    for invalid in (float("inf"), float("nan"), -1):
        with pytest.raises(GatewayProtocolError):
            parse_request_params("application.read", {"operation": "history", "arguments": {"since": invalid}})


@pytest.mark.parametrize("stop_generation,stop_offset,expected", [(6, 1, "applied"), (7, -1, "applied"), (7, 1, "unknown")])
def test_settings_stop_marker_belongs_to_current_loaded_generation(management, stop_generation, stop_offset, expected):
    store = ObservationStore(management.store.root, writable=True)
    config_id = store.put_configuration({"application_settings": management.loaded_revisions()})
    now = time.time()
    store.set_meta("loaded_configuration", {"config_id": config_id, "generation": 7, "observed_at": now})
    store.set_meta("runtime_status", {"state": "stopped", "generation": stop_generation, "observed_at": now + stop_offset})
    result = management.settings()
    assert result["prompts"]["status"] == expected
    assert result["model"]["status"] == expected
