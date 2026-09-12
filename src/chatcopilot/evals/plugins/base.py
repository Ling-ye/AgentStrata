"""Public contracts for repository-owned evaluation plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from chatcopilot.evals.models import EvalCase, SuiteManifest

PLUGIN_API_VERSION = "2"
STATIC_PLUGIN_BINDING_VERSION = "agentstrata-plugin-binding/v2"
DRIVER_PROTOCOL_VERSION = "agentstrata-eval-driver/v2"
SCORER_PROTOCOL_VERSION = "agentstrata-eval-scorer/v2"


@dataclass(frozen=True)
class CaseLoadContext:
    """Side-effect policy and validated options available while loading cases."""

    manifest: SuiteManifest
    auto_prepare: bool = True
    options: Mapping[str, Any] | None = None


CaseLoader = Callable[[CaseLoadContext], tuple[EvalCase, ...]]
PluginHook = Callable[..., Any]


@dataclass(frozen=True)
class EvaluationPlugin:
    """Trusted behavior bound to declarative manifests by exact plugin id.

    Core owns lifecycle, workspaces and authoritative artifacts. Hooks receive
    Core-created contexts and must not create a second lifecycle or artifact root.
    """

    plugin_id: str
    api_version: str
    implementation_module: str
    allowed_drivers: frozenset[str]
    load_cases: CaseLoader
    preflight: PluginHook | None = None
    prepare: PluginHook | None = None
    build_task: PluginHook | None = None
    execute_model: PluginHook | None = None
    open_case: PluginHook | None = None
    judge: PluginHook | None = None
    cleanup: PluginHook | None = None


__all__ = [
    "CaseLoadContext",
    "CaseLoader",
    "DRIVER_PROTOCOL_VERSION",
    "EvaluationPlugin",
    "PLUGIN_API_VERSION",
    "PluginHook",
    "SCORER_PROTOCOL_VERSION",
    "STATIC_PLUGIN_BINDING_VERSION",
]
