"""Repository-owned evaluation plugin contracts and static catalog."""

from chatcopilot.evals.plugins.base import (
    DRIVER_PROTOCOL_VERSION,
    PLUGIN_API_VERSION,
    SCORER_PROTOCOL_VERSION,
    STATIC_PLUGIN_BINDING_VERSION,
    CaseLoadContext,
    EvaluationPlugin,
)
from chatcopilot.evals.plugins.catalog import (
    PluginBinding,
    get_evaluation_plugin,
    get_plugin_binding,
    list_plugin_bindings,
    plugin_binding_snapshot,
    plugin_implementation_sha256,
)

__all__ = [
    "CaseLoadContext",
    "DRIVER_PROTOCOL_VERSION",
    "EvaluationPlugin",
    "PLUGIN_API_VERSION",
    "PluginBinding",
    "get_evaluation_plugin",
    "get_plugin_binding",
    "list_plugin_bindings",
    "plugin_binding_snapshot",
    "plugin_implementation_sha256",
    "SCORER_PROTOCOL_VERSION",
    "STATIC_PLUGIN_BINDING_VERSION",
]
