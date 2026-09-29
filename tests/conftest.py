"""Isolated, explicitly configured model host for repository tests."""
import pytest


@pytest.fixture(autouse=True)
def model_settings(tmp_path_factory, monkeypatch):
    """Unit tests use a configured test host and never read an operator's model file.

    Tests for an unconfigured host explicitly point at an empty/missing file.
    Secrets are deliberately absent; this fixture never enables a real API call.
    """
    import os
    from chatcopilot.core import model_settings as settings
    from tests.model_settings_fixture import test_model_document, write_models
    path = tmp_path_factory.mktemp("model-host") / "host-models.json"
    write_models(path, test_model_document())
    original = settings.settings_path
    def resolve(environment=None):
        env = os.environ if environment is None else environment
        return original(env) if env.get("AGENTSTRATA_LLM_CONFIG") else path
    monkeypatch.setattr(settings, "settings_path", resolve)
    monkeypatch.setenv("AGENTSTRATA_LLM_CONFIG", str(path))
    # Bundled BotSpecs are test inputs; their ignored operator local.env is not.
    from pathlib import Path
    from chatcopilot.evals import evaluation_runtime, execution_support
    original_load = execution_support.load_local_env
    repository_bots = Path(__file__).resolve().parents[1] / "bots"
    def isolated_load(source):
        if Path(source).resolve().is_relative_to(repository_bots) and Path(source).name == "local.env":
            return None
        return original_load(source)
    monkeypatch.setattr(evaluation_runtime, "load_local_env", isolated_load)
    monkeypatch.setattr(execution_support, "load_local_env", isolated_load)
    from chatcopilot.evals.application import bots
    original_values = bots.load_local_env_values
    def isolated_values(source, **kwargs):
        if Path(source).resolve().is_relative_to(repository_bots) and Path(source).name == "local.env":
            return {}
        return original_values(source, **kwargs)
    monkeypatch.setattr(bots, "load_local_env_values", isolated_values)
    return path


@pytest.fixture
def api_model_settings(model_settings, monkeypatch):
    """A service test can preflight both Native and Codex without a real login."""
    import json
    from tests.model_settings_fixture import write_models
    data = json.loads(model_settings.read_text())
    data["connections"]["service-api"] = {"kind": "openai_responses",
        "auth": {"mode": "api_key", "key_env": "CHATCOPILOT_LINGYE_API_KEY"}}
    data["profiles"]["codex-main"]["connection"] = "service-api"
    write_models(model_settings, data)
    monkeypatch.setenv("CHATCOPILOT_LINGYE_API_KEY", "synthetic-service-credential")


