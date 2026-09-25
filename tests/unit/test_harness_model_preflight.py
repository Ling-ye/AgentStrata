"""Native worker catalog checks, without a Harness-maintained allowlist."""
import pytest

from chatcopilot.harness.codex_adapter import preflight_worker_model, require_available_model
from chatcopilot.harness.models import HarnessError


MODELS = [{"model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
          {"model": "gpt-6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"},
                                                               {"reasoningEffort": "high"}]}]


def test_old_worker_catalog_rejects_requested_model():
    with pytest.raises(HarnessError) as error:
        require_available_model(MODELS[:1], "gpt-6-sol", "high")
    assert error.value.code == "model_unavailable"


def test_current_worker_catalog_accepts_explicit_model_and_effort():
    require_available_model(MODELS, "gpt-6-sol", "high")


def test_unsupported_effort_is_distinct():
    with pytest.raises(HarnessError) as error:
        require_available_model(MODELS, "gpt-6-sol", "max")
    assert error.value.code == "model_effort_unsupported"


def test_catalog_failure_is_not_treated_as_model_rejection(monkeypatch, tmp_path):
    def fail(*args):
        raise HarnessError("model_probe_unavailable", "catalog unavailable")
    monkeypatch.setattr("chatcopilot.harness.codex_adapter.worker_models", fail)
    with pytest.raises(HarnessError) as error:
        preflight_worker_model({}, tmp_path, "gpt-6-sol", "high")
    assert error.value.code == "model_probe_unavailable"
