from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.model_catalog import ModelCatalog, normalize_models
from chatcopilot.core.model_settings import (
    ModelSettingsConflict, ModelSettingsError, ModelSettingsStore, empty_settings,
    frozen_settings, read_settings, resolve_binding, validate_settings,
)


def document():
    return {"connections": {"api": {"kind": "openai_compatible", "base_url": "https://example.test/v1",
        "auth": {"mode": "api_key", "key_env": "EXAMPLE_KEY"}}},
        "profiles": {"daily": {"connection": "api", "model": "discovered"}},
        "bindings": {"chat": "daily", "research": "daily", "evaluation.judge": "daily"}}


def test_one_configuration_resolves_all_consumers_without_copying_secrets(tmp_path):
    store = ModelSettingsStore(tmp_path / "llm.json")
    data = document()
    store.save(data, revision=digest(empty_settings()))
    environment = {"AGENTSTRATA_LLM_CONFIG": str(store.path), "EXAMPLE_KEY": "private-test-key"}
    chat = resolve_binding("chat", environment=environment)
    judge = resolve_binding("evaluation.judge", environment=environment)
    assert chat.model_route() == judge.model_route()
    assert chat.api_key == "private-test-key"
    assert "private-test-key" not in store.path.read_text()
    assert "private-test-key" not in repr(chat)
    data["profiles"]["unused"] = {"connection": "api", "model": "other"}
    assert resolve_binding("chat", document=data, environment=environment).model_route().fingerprint == chat.model_route().fingerprint


def test_atomic_save_rejects_concurrent_overwrite(tmp_path):
    store = ModelSettingsStore(tmp_path / "llm.json")
    version = store.view()["revision"]
    def save(model):
        data = document()
        data["profiles"]["daily"]["model"] = model
        try:
            return store.save(data, revision=version)["profiles"]["daily"]["model"]
        except ModelSettingsConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["first", "second"]))
    assert results.count("conflict") == 1
    assert store.read()["profiles"]["daily"]["model"] in {"first", "second"}
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_failed_validation_does_not_change_saved_settings(tmp_path):
    store = ModelSettingsStore(tmp_path / "llm.json")
    saved = store.save(document(), revision=store.view()["revision"])
    invalid = document()
    invalid["profiles"]["daily"]["context_window"] = 1000000
    with pytest.raises(ModelSettingsError):
        store.save(invalid, revision=saved["revision"])
    assert store.view() == saved
    with pytest.raises(ModelSettingsError, match="未配置"):
        resolve_binding("absent", document=document())


def test_catalog_distinguishes_unknown_capabilities_and_retains_success():
    state = {"fail": False, "identity": "account-one"}
    def discover(_connection):
        if state["fail"]:
            raise RuntimeError("private provider failure")
        return [{"id": "discovered"}]
    catalog = ModelCatalog(discover, lambda _: state["identity"])
    connection = document()["connections"]["api"]
    fresh = catalog.refresh(connection)
    assert fresh["models"][0]["reasoning_efforts"] is None
    assert fresh["models"][0]["context_window"] is None
    catalog.validate_save(document(), empty_settings())
    invalid = document()
    invalid["profiles"]["daily"]["reasoning_effort"] = "high"
    with pytest.raises(ModelSettingsError, match="未声明"):
        catalog.validate_save(invalid, empty_settings())
    state["fail"] = True
    stale = catalog.refresh(connection)
    assert stale["models"] == fresh["models"] and stale["fetched_at"] == fresh["fetched_at"]
    assert "private provider failure" not in stale["error"]
    state["identity"] = "account-two"
    assert catalog.get(connection)["models"] == []


def test_new_effort_comes_from_the_interface_without_a_host_enum():
    rows = normalize_models([{"model": "new-model", "supportedReasoningEfforts": [
        {"reasoningEffort": "future-effort"}], "defaultReasoningEffort": "future-effort"}], codex=True)
    assert rows[0]["reasoning_efforts"] == ["future-effort"]
    from chatcopilot.contracts.model_selection import WorkerModelProfile
    assert WorkerModelProfile("new-model", "future-effort").reasoning_effort == "future-effort"


def test_session_choices_do_not_cross_connections_with_the_same_key_name():
    from chatcopilot.core.model_settings import profile_choices, resolve_profile
    data = document()
    data["connections"]["other-account"] = {**data["connections"]["api"], "env_file": "/private/another-account.env"}
    data["profiles"]["other"] = {"connection": "other-account", "model": "discovered"}
    cfg = resolve_profile("daily", document=data, environment={})
    choices = profile_choices(document=data, base=cfg.model_route(), connection_id=cfg.connection_id)
    assert set(choices) == {"daily"}


def test_malformed_display_metadata_never_enters_the_frontend():
    row = normalize_models([{"id": "model", "name": {"unsafe": "value"}}])[0]
    assert row["name"] == "model"


def test_job_capture_survives_later_saves(tmp_path, monkeypatch):
    store = ModelSettingsStore(tmp_path / "llm.json")
    saved = store.save(document(), revision=store.view()["revision"])
    monkeypatch.setenv("AGENTSTRATA_LLM_CONFIG", str(store.path))
    captured = read_settings()
    changed = deepcopy(captured)
    changed["profiles"]["daily"]["model"] = "new-model"
    store.save(changed, revision=saved["revision"])
    with frozen_settings(captured):
        assert resolve_binding("chat").model == "discovered"
    assert resolve_binding("chat").model == "new-model"


def test_request_uses_saved_effort(monkeypatch):
    from chatcopilot.core.model_routes import create_model_client
    data = document()
    data["profiles"]["daily"]["reasoning_effort"] = "future-effort"
    cfg = resolve_binding("chat", document=data, environment={"EXAMPLE_KEY": "test-key"})
    client = create_model_client(cfg)
    seen = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop")], usage=None)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: seen.append(kw) or response)))
    assert client.chat([{"role": "user", "content": "hello"}], stream=False).content == "ok"
    assert seen[0]["reasoning_effort"] == "future-effort"


def test_console_direct_requests_cannot_add_undiscovered_parameters(tmp_path):
    from console.backend.routes.llm import router
    from console.control.llm import ModelControl
    catalog = ModelCatalog(lambda _: [{"id": "discovered"}], lambda _: "test-account")
    store = ModelSettingsStore(tmp_path / "llm.json")
    app = FastAPI()
    app.state.llm = ModelControl(tmp_path, store=store, catalog=catalog)
    app.include_router(router)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        version = client.get("/api/llm/config").json()["revision"]
        data = document()
        assert client.put("/api/llm/config", json={**data, "revision": version}).status_code == 400
        assert client.post("/api/llm/catalog/refresh", json={"connection": "api", "draft": data["connections"]["api"]}).status_code == 200
        assert client.put("/api/llm/config", json={**data, "revision": version}).status_code == 200
        assert client.put("/api/llm/config", json={**data, "revision": version}).status_code == 409
        data["profiles"]["daily"]["reasoning_effort"] = "invented"
        version = client.get("/api/llm/config").json()["revision"]
        assert client.put("/api/llm/config", json={**data, "revision": version}).status_code == 400
        assert client.post("/api/llm/catalog/refresh", json={"connection": "api"}, headers={"Origin": "https://foreign.test"}).status_code == 403


@pytest.mark.parametrize("field", ["api_key", "temperature", "context_window"])
def test_unknown_or_secret_fields_rejected(field):
    data = document()
    data["profiles"]["daily"][field] = "unexpected"
    with pytest.raises(ModelSettingsError):
        validate_settings(data)


@pytest.mark.parametrize("field", ["kind", "base_url", "env_file", "codex_bin_env", "credential_root_env"])
def test_malformed_connection_fields_return_a_configuration_error(field):
    data = document()
    data["connections"]["api"][field] = ["invalid"]
    with pytest.raises(ModelSettingsError):
        validate_settings(data)


def test_malformed_auth_reference_returns_a_configuration_error():
    data = document()
    data["connections"]["api"]["auth"]["key_env"] = 123
    with pytest.raises(ModelSettingsError):
        validate_settings(data)


def test_retired_topic_model_cannot_override_the_shared_route(tmp_path):
    from chatcopilot.core.config import load_config
    path = tmp_path / "runtime.yaml"
    path.write_text("runtime:\n  topic_model: retired-model\n")
    with pytest.raises(ValueError, match="旧模型"):
        load_config(path, environment={})


@pytest.mark.parametrize("systemd", [False, True])
def test_isolated_worker_projects_its_frozen_connection_environment(tmp_path, monkeypatch, systemd):
    import json
    from chatcopilot.external_tools.dev import code_task_runtime as runtime
    data = {"connections": {"worker": {"kind": "codex", "auth": {"mode": "chatgpt", "profile": "worker"},
        "codex_bin_env": "CUSTOM_CODEX_BIN", "credential_root_env": "CUSTOM_CODEX_HOME"}},
        "profiles": {"captured": {"connection": "worker", "model": "captured-model"}},
        "bindings": {"code": "captured"}}
    ModelSettingsStore(tmp_path / "llm.json").save(data, revision=digest(empty_settings()))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"job_id": "test-frozen-worker"}))
    monkeypatch.setenv("CUSTOM_CODEX_BIN", "/opt/worker/codex")
    monkeypatch.setenv("CUSTOM_CODEX_HOME", "/private/worker")
    monkeypatch.setenv("CHATCOPILOT_CODE_BINDING", "changed-live-binding")
    monkeypatch.delenv("CHATCOPILOT_CODE_TASK_DISABLE_SYSTEMD", raising=False)
    monkeypatch.setattr(runtime, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_process_start_ticks", lambda _pid: 123)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: "/bin/systemd-run" if systemd else None)
    observed = {}
    def run(command, **kwargs):
        observed.update(part.removeprefix("--setenv=").split("=", 1) for part in command if part.startswith("--setenv="))
        return SimpleNamespace(returncode=0)
    def popen(command, **kwargs):
        observed.update(kwargs["env"])
        return SimpleNamespace(pid=123456789)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    runtime.schedule_code_task_worker(request)
    cfg = resolve_binding(observed["CHATCOPILOT_CODE_BINDING"], environment=observed)
    assert cfg.model == "captured-model"
    assert cfg.codex_bin == "/opt/worker/codex"
    assert cfg.credential_root == "/private/worker"
    assert observed["AGENTSTRATA_LLM_CONFIG"] == str(tmp_path / "llm.json")


@pytest.mark.parametrize("api", ["chat_completions", "openai_responses"])
@pytest.mark.parametrize("selected", [None, "", "future-effort"])
def test_profile_switch_can_clear_the_previous_effort(monkeypatch, api, selected):
    from dataclasses import replace
    from chatcopilot.contracts.model_runtime import ModelRequest
    from chatcopilot.core.model_config import LLMConfig
    from chatcopilot.core.model_routes import create_model_client
    cfg = LLMConfig(model="fixture", api=api, api_key="fixture-key", reasoning_effort="high")
    client = create_model_client(cfg)
    observed = []
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop")], usage=None)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: observed.append(kw) or completion)))
    response = SimpleNamespace(status_code=200, close=lambda: None, iter_lines=lambda: iter([
        b'data: {"type":"response.completed","response":{"output":[]}}', b'']))
    monkeypatch.setattr("chatcopilot.core.responses_client.requests.post",
                        lambda *args, **kw: observed.append(kw["json"]) or response)
    chat = client.chat
    monkeypatch.setattr(client, "chat", lambda *args, **kw: chat(*args, **{**kw, "stream": False}))
    client.chat([], reasoning_effort=selected)
    expected = "high" if selected is None else selected
    wire = observed.pop()
    if api == "chat_completions":
        assert wire.get("reasoning_effort") == (expected or None)
    else:
        assert wire.get("reasoning") == ({"effort": expected} if expected else None)
    # The typed route is complete: None there explicitly selects provider defaults.
    client.complete(ModelRequest(replace(cfg.model_route(), reasoning_effort=None), ()))
    wire = observed.pop()
    assert "reasoning_effort" not in wire and "reasoning" not in wire
