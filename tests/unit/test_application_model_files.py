"""File-first desktop configuration uses the production model and credential loaders."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatcopilot.core.application_installation import RECORD, SCHEMA, installation
from chatcopilot.core.config import load_config
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.model_settings import ModelSettingsConflict, ModelSettingsError, resolve_binding
from chatcopilot.deployment.application_models import model_settings
from chatcopilot.deployment.configuration import runtime_environment


@pytest.fixture
def application(tmp_path):
    root = tmp_path / "application"
    write_private(root / "bots/live2d-desktop/bot.yaml",
                  "id: live2d-desktop\nagent_profile: agent.yaml\n")
    write_private(root / "bots/live2d-desktop/agent.yaml", "llm:\n  chat:\n    binding: chat\n")
    write_private(root / RECORD, json.dumps({"schema": SCHEMA, "bot_id": "live2d-desktop",
        "bot_relative": "bots/live2d-desktop/bot.yaml", "delivered_digest": "a" * 64}))
    return root


def file_configuration(root, *, encoding="utf-8"):
    control = installation(root).control
    config = control / "llm.json"
    credential = control / "model-credentials/openai.env"
    example = Path(__file__).resolve().parents[2] / "integrations/live2d_desktop/config/llm.example.json"
    document = json.loads(example.read_text(encoding="utf-8"))
    document["connections"]["openai"]["env_file"] = str(credential)
    document["profiles"]["desktop"]["model"] = "file-fixture-model"
    write_private(credential, 'AGENTSTRATA_DESKTOP_API_KEY="fixture-file-credential"\n')
    # Editors may save UTF-8 with BOM on Windows.
    credential.write_text(credential.read_text(encoding="utf-8"), encoding=encoding)
    write_private(config, json.dumps(document))
    config.write_text(json.dumps(document), encoding=encoding)
    write_private(control / "runtime.env",
                  "AGENTSTRATA_LLM_CONFIG=" + json.dumps(str(config)) + "\n")
    return config, credential, document


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_startup_reads_files_without_settings_save_and_reloads_on_restart(application, encoding, monkeypatch):
    config, credential, document = file_configuration(application, encoding=encoding)
    monkeypatch.setenv("AGENTSTRATA_DESKTOP_API_KEY", "fixture-inherited-credential")
    environment = runtime_environment(installation(application).control / "runtime.env")
    before = config.read_bytes()
    loaded = load_config(environment=environment, default_paths=())
    assert loaded.llm.model == "file-fixture-model"
    assert loaded.llm.base_url == "https://api.openai.com/v1"
    assert loaded.llm.api == "chat_completions"
    assert loaded.llm.api_key == "fixture-file-credential"
    assert config.read_bytes() == before
    assert model_settings(application)["configured"]
    assert "fixture-file-credential" not in json.dumps(model_settings(application))
    assert "fixture-file-credential" not in repr(loaded.llm)

    document["profiles"]["desktop"]["model"] = "edited-fixture-model"
    config.write_text(json.dumps(document), encoding=encoding)
    credential.write_text('AGENTSTRATA_DESKTOP_API_KEY="fixture-edited-credential"\n', encoding=encoding)
    reloaded = load_config(environment=environment, default_paths=())
    assert reloaded.llm.model == "edited-fixture-model"
    assert reloaded.llm.api_key == "fixture-edited-credential"


def test_unconfigured_application_does_not_create_settings_or_use_host_binding(application):
    config = installation(application).control / "llm.json"
    view = model_settings(application)
    assert view["configured"] is False
    assert view["model"] == ""
    assert not config.exists()
    with pytest.raises(ModelSettingsError, match="chat"):
        resolve_binding("chat", environment={"AGENTSTRATA_LLM_CONFIG": str(config)})


def test_missing_credential_file_does_not_use_inherited_key(application):
    config, credential, _ = file_configuration(application)
    credential.unlink()
    with pytest.raises(FileNotFoundError):
        resolve_binding("chat", environment={"AGENTSTRATA_LLM_CONFIG": str(config),
            "AGENTSTRATA_DESKTOP_API_KEY": "fixture-inherited-credential"})


def test_malformed_configuration_does_not_fall_back_to_defaults(application):
    config, _, document = file_configuration(application)
    config.with_suffix(".defaults.json").write_text(json.dumps(document))
    config.write_text("{")
    with pytest.raises(ValueError):
        model_settings(application)
    with pytest.raises(ValueError):
        load_config(environment={"AGENTSTRATA_LLM_CONFIG": str(config)}, default_paths=())


def test_model_only_ui_save_keeps_manual_credential_reference_without_reading_secret(application, monkeypatch):
    config, credential, document = file_configuration(application)
    document["connections"]["openai"]["auth"]["key_env"] = "CUSTOM_DESKTOP_KEY"
    credential.write_text('CUSTOM_DESKTOP_KEY="fixture-custom-credential"\n')
    config.write_text(json.dumps(document))
    before = credential.read_bytes()
    initial = model_settings(application)
    with monkeypatch.context() as patch:
        patch.setattr("chatcopilot.core.settings.load_local_env_values",
                      lambda *a, **kw: pytest.fail("Model settings must not read credential values"))
        saved = model_settings(application, {"kind": initial["kind"], "model": "ui-fixture-model",
            "base_url": initial["base_url"], "revision": initial["revision"]})
    assert saved["model"] == "ui-fixture-model"
    current = json.loads(config.read_text())
    route = current["connections"][current["profiles"][current["bindings"]["chat"]]["connection"]]
    assert route["env_file"] == str(credential)
    assert route["auth"]["key_env"] == "CUSTOM_DESKTOP_KEY"
    assert credential.read_bytes() == before
    assert list(credential.parent.iterdir()) == [credential]
    assert resolve_binding("chat", environment={"AGENTSTRATA_LLM_CONFIG": str(config)}).api_key == "fixture-custom-credential"


def test_changed_endpoint_requires_its_own_credential(application):
    config, credential, _ = file_configuration(application)
    initial = model_settings(application)
    before = config.read_bytes(), credential.read_bytes()
    with pytest.raises(ValueError, match="API credential"):
        model_settings(application, {"kind": initial["kind"], "model": initial["model"],
            "base_url": "https://other.invalid/v1", "revision": initial["revision"]})
    assert (config.read_bytes(), credential.read_bytes()) == before


def test_explicit_ui_key_uses_managed_store_and_leaves_manual_file_untouched(application):
    config, credential, _ = file_configuration(application)
    initial = model_settings(application)
    before = credential.read_bytes()
    saved = model_settings(application, {"kind": initial["kind"], "model": initial["model"],
        "base_url": initial["base_url"], "api_key": "fixture-ui-credential", "revision": initial["revision"]})
    assert credential.read_bytes() == before
    assert "fixture-ui-credential" not in json.dumps(saved)
    assert "fixture-ui-credential" not in config.read_text()
    assert resolve_binding("chat", environment={"AGENTSTRATA_LLM_CONFIG": str(config)}).api_key == "fixture-ui-credential"
    # Existing managed credentials also survive a later model-only save.
    model_settings(application, {"kind": saved["kind"], "model": "second-ui-fixture-model",
        "base_url": saved["base_url"], "revision": saved["revision"]})
    assert resolve_binding("chat", environment={"AGENTSTRATA_LLM_CONFIG": str(config)}).api_key == "fixture-ui-credential"


def test_manual_edit_conflicts_with_stale_ui_save_before_writing_key(application):
    config, credential, document = file_configuration(application)
    initial = model_settings(application)
    document["profiles"]["desktop"]["model"] = "new-file-model"
    config.write_text(json.dumps(document))
    before = config.read_bytes(), credential.read_bytes()
    with pytest.raises(ModelSettingsConflict):
        model_settings(application, {"kind": initial["kind"], "model": "stale-ui-model",
            "base_url": initial["base_url"], "api_key": "fixture-stale-credential", "revision": initial["revision"]})
    assert (config.read_bytes(), credential.read_bytes()) == before
    assert list(credential.parent.iterdir()) == [credential]


def test_chatgpt_ui_configuration_still_uses_the_shared_model_store(application):
    initial = model_settings(application)
    saved = model_settings(application, {"kind": "chatgpt", "model": "fixture-chatgpt-model",
        "revision": initial["revision"]})
    assert saved["configured"]
    assert saved["kind"] == "chatgpt"
    assert not (installation(application).control / "model-credentials").exists()
