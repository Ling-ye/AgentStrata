"""Frozen bridge scenarios and the exact repository-owned adapter under test."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.contracts.model_runtime import digest
from chatcopilot.evals.models import EvalCaseDefinition, EvaluationError
from chatcopilot.evals.result_codec import PipelineFailure

SUITE_ID = "agentstrata-application-bridges-v1"
_SCENARIOS = {
    "dialogue": {"live2d", "qq"},
    "avatar-action": {"live2d"},
    "session-isolation": {"live2d"},
    "admission": {"qq"},
    "delivery-unknown": {"qq"},
}
_LIVE2D_ROOT = "integrations/open_llm_vtuber/agentstrata_vtuber"
_LIVE2D_FILES = ("__init__.py", "bridge.py", "avatar.py", "config.py", "control.py")


def product_root() -> Path:
    # The active package is inside the service's frozen code_source when one is selected.
    # Never fall back to an operator's unrelated checkout or installed client directory.
    return Path(__file__).resolve().parents[3]


def component_identity() -> dict:
    root = product_root()
    members = {}
    for name in (*("src/chatcopilot/channels/qq_onebot/" + part for part in
                   ("driver.py", "codec.py", "config.py", "resources.py")),
                 *(_LIVE2D_ROOT + "/" + part for part in _LIVE2D_FILES)):
        path = root / name
        if not path.is_file():
            raise ValueError("Bridge evaluation requires the service repository's managed adapter sources")
        members[name] = trusted_source_sha256(path, root=root, max_bytes=max(1, path.stat().st_size))
    return {"schema": "agentstrata-bridge-components/v1", "members": members, "sha256": digest(members)}


def live2d_module(name: str):
    if name not in {"bridge", "avatar", "config", "control"}:
        raise ValueError("unknown bridge module")
    component_identity()
    package = product_root() / _LIVE2D_ROOT
    search = str(package.parent)
    sys.path.insert(0, search)
    try:
        module = importlib.import_module("agentstrata_vtuber." + name)
    finally:
        sys.path.remove(search)
    if Path(module.__file__).resolve() != package / (name + ".py"):
        raise ValueError("Live2D module does not match the evaluated source")
    return module


def validate(definition: EvalCaseDefinition) -> None:
    params = definition.scenario_params
    application = params.get("application")
    if (definition.plugin_id != "application-bridge" or definition.driver_id != "agent_configured"
            or not isinstance(application, str) or application not in _SCENARIOS.get(definition.scenario_id, set())
            or set(params) - {"application", "expected_text"}
            or not isinstance(params.get("expected_text", ""), str)
            or len(definition.assertions) != 1
            or definition.assertions[0].assertion_id != "application_bridge"
            or definition.assertions[0].arguments
            or definition.quality.get("enabled") is not False):
        raise ValueError("invalid frozen application-bridge scenario")
    if definition.scenario_id == "dialogue" and not params.get("expected_text"):
        raise ValueError("dialogue requires a frozen expected text")


def unavailable(message: str, *, code: str = "environment_error") -> PipelineFailure:
    return PipelineFailure(EvaluationError("execution", code, message))
