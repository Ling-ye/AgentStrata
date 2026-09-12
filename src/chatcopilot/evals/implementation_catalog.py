"""Static execution/scoring implementation identity for Evaluation fingerprints."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from chatcopilot.core.file_integrity import trusted_source_sha256


IMPLEMENTATION_CATALOG_VERSION = "agentstrata-eval-implementation/v1"
RUNTIME_IMPLEMENTATION_CATALOG_VERSION = "agentstrata-runtime-implementation/v1"
_TRUSTED_NAMESPACE = "chatcopilot.evals."
_TRUSTED_PACKAGE_NAMESPACE = "chatcopilot."
_MODULE_PART_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_EVALS_ROOT = Path(__file__).absolute().parent
_PACKAGE_ROOT = _EVALS_ROOT.parent
_MAX_SOURCE_BYTES = 4 * 1024 * 1024

_COMMON_SUITE_MODULES = (
    "chatcopilot.core.file_integrity",
    "chatcopilot.evals.private_files",
    "chatcopilot.evals.evaluations",
    "chatcopilot.evals.evaluation_runtime",
    "chatcopilot.evals.judges",
    "chatcopilot.evals.runner",
    "chatcopilot.evals.workbench",
    "chatcopilot.evals.benchmark_scoring",
    "chatcopilot.evals.deepeval_engine",
)
_CASE_IMPLEMENTATIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("agent-tasks", "agent_configured"): (
        "chatcopilot.evals.agent_tasks.scenes", "chatcopilot.evals.agent_tasks.runtime",
        "chatcopilot.evals.agent_tasks.verifier", "chatcopilot.evals.agent_tasks.code_fixture",
        "chatcopilot.evals.capability_executor", "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.deepeval_engine", "chatcopilot.evals.trial_capture",
    ),
    ("business-agent", "agent_configured"): (
        "chatcopilot.evals.business_dataset", "chatcopilot.evals.business_tools",
        "chatcopilot.evals.business_scoring", "chatcopilot.evals.business_policy", "chatcopilot.evals.deepeval_mapping",
        "chatcopilot.evals.environment_agent", "chatcopilot.evals.trial_capture",
    ),
    ("generic-agent", "agent_isolated"): (
        "chatcopilot.evals.deepeval_engine",
        "chatcopilot.evals.business_cases",
        "chatcopilot.evals.business_verifiers",
        "chatcopilot.evals.ifeval_subset",
        "chatcopilot.evals.trial_capture",
        "chatcopilot.evals.capability_executor",
        "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.isolated_executor",
    ),
    ("generic-agent", "agent_configured"): (
        "chatcopilot.evals.deepeval_engine",
        "chatcopilot.evals.business_cases",
        "chatcopilot.evals.business_verifiers",
        "chatcopilot.evals.ifeval_subset",
        "chatcopilot.evals.trial_capture",
        "chatcopilot.evals.capability_executor",
        "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.fx_oracle",
        "chatcopilot.evals.isolated_executor",
    ),
    ("acp-scenario", "acp_scenario"): (
        "chatcopilot.evals.capability_executor",
        "chatcopilot.evals.capability_scenarios",
        "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.isolated_executor",
    ),
    ("qq-message-flow", "qq_message_flow"): (
        "chatcopilot.evals.capability_executor",
        "chatcopilot.evals.capability_scenarios",
        "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.isolated_executor",
        "chatcopilot.evals.qq_flow_scenarios",
        "chatcopilot.botspec.session_env",
        "chatcopilot.core.allowlists",
        "chatcopilot.core.persona_control",
        "chatcopilot.core.persistent_state",
        "chatcopilot.core.session_env_store",
        "chatcopilot.middleware.acp.admission",
        "chatcopilot.middleware.acp.agent_bridge",
        "chatcopilot.middleware.acp.event_translator",
        "chatcopilot.evals.qq_ingress_probe",
        "chatcopilot.channels.qq_onebot.codec",
        "chatcopilot.channels.qq_onebot.driver",
        "chatcopilot.channels.qq_onebot.config",
        "chatcopilot.middleware.acp.group_conversation",
        "chatcopilot.agent.persona.tools",
        "chatcopilot.middleware.acp.server",
        "chatcopilot.middleware.acp.transport_attestation",
        "chatcopilot.middleware.acp.turn_orchestrator",
        "chatcopilot.middleware.acp.workspace_service",
        "chatcopilot.middleware.runtime.tasks",
    ),
    ("swe-bench", "agent_configured"): (
        "chatcopilot.evals.benchmark_data",
        "chatcopilot.evals.adapters.swebench", "chatcopilot.evals.adapters.swebench_runtime",
        "chatcopilot.evals.environment_agent", "chatcopilot.evals.environment_cleanup", "chatcopilot.evals.trial_capture",
    ),
    ("agentbench-fc", "agent_configured"): (
        "chatcopilot.evals.adapters.agentbench", "chatcopilot.evals.environment_agent",
        "chatcopilot.evals.environment_cleanup", "chatcopilot.evals.trial_capture",
    ),
    ("gaia", "agent_configured"): (
        "chatcopilot.evals.benchmark_data",
        "chatcopilot.evals.adapters.gaia",
        "chatcopilot.evals.judges_llm",
    ),
    ("ifeval", "direct_llm"): ("chatcopilot.evals.adapters.ifeval", "chatcopilot.evals.ifeval_official", "chatcopilot.evals.ifeval_language", "chatcopilot.evals.vendor.ifeval.instructions", "chatcopilot.evals.vendor.ifeval.instructions_util", "chatcopilot.evals.vendor.ifeval.instructions_registry"),
    ("bfcl", "direct_llm"): ("chatcopilot.evals.adapters.bfcl",),
}
_COMPARISON_IMPLEMENTATIONS = (
    "chatcopilot.core.file_integrity",
    "chatcopilot.evals.private_files",
    "chatcopilot.evals.adapters.gaia",
    "chatcopilot.evals.adapters.ifeval",
    "chatcopilot.evals.isolated_executor",
    "chatcopilot.evals.profiles",
    "chatcopilot.evals.runner",
)
_COMMON_RUNTIME_IMPLEMENTATIONS = (
    "chatcopilot.core.file_integrity",
    "chatcopilot.application.agent_runtime",
    "chatcopilot.botspec.runtime_env",
    "chatcopilot.core.config",
    "chatcopilot.core.llm_client",
    "chatcopilot.core.visible_model_response",
    "chatcopilot.contracts.agent",
    "chatcopilot.agent.backends.registry",
    "chatcopilot.agent.capabilities.assembly",
    "chatcopilot.agent.capabilities.delegation",
    "chatcopilot.agent.capabilities.unified_search",
    "chatcopilot.agent.runtime",
    "chatcopilot.agent.search.coordinator",
    "chatcopilot.agent.search.providers",
    "chatcopilot.agent.search.router",
    "chatcopilot.agent.search.tool",
    "chatcopilot.agent.subagents.registry",
    "chatcopilot.agent.subagents.result",
    "chatcopilot.agent.subagents.runner",
    "chatcopilot.agent.subagents.search_circuit",
    "chatcopilot.agent.tools.executor",
    "chatcopilot.agent.tools.registry",
    "chatcopilot.agent.turn",
)
_BACKEND_RUNTIME_IMPLEMENTATIONS: dict[str, tuple[str, ...]] = {
    "codex": (
        "chatcopilot.agent.backends.codex",
        "chatcopilot.agent.backends.codex_events",
        "chatcopilot.agent.backends.codex_permissions",
        "chatcopilot.agent.backends.session_relay",
    ),
    "direct": ("chatcopilot.core.llm_client",),
    "langgraph": ("chatcopilot.agent.backends.inprocess",),
    "native": ("chatcopilot.agent.backends.inprocess",),
    "none": (),
}


def trusted_module_sha256(module_name: str) -> str:
    """Hash one exact package-owned source file without importing the module."""

    if not module_name.startswith(_TRUSTED_NAMESPACE):
        raise ValueError(f"evaluation implementation is outside trusted namespace: {module_name}")
    return _trusted_source_sha256(
        module_name,
        namespace=_TRUSTED_NAMESPACE,
        root=_EVALS_ROOT,
        scope="evaluation implementation",
    )


def trusted_runtime_module_sha256(module_name: str) -> str:
    """Hash one repository-owned runtime source file without importing it."""

    if not module_name.startswith(_TRUSTED_PACKAGE_NAMESPACE):
        raise ValueError(f"runtime implementation is outside trusted namespace: {module_name}")
    return _trusted_source_sha256(
        module_name,
        namespace=_TRUSTED_PACKAGE_NAMESPACE,
        root=_PACKAGE_ROOT,
        scope="runtime implementation",
    )


def _trusted_source_sha256(
    module_name: str,
    *,
    namespace: str,
    root: Path,
    scope: str,
) -> str:
    relative_parts = module_name.removeprefix(namespace).split(".")
    if not relative_parts or any(not _MODULE_PART_RE.fullmatch(part) for part in relative_parts):
        raise ValueError(f"{scope} module name is invalid: {module_name}")
    path = root / Path(*relative_parts).with_suffix(".py")
    try:
        return trusted_source_sha256(path, root=root, max_bytes=_MAX_SOURCE_BYTES)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{scope} {module_name}: {exc}") from exc


def suite_implementation_snapshot(
    bindings: Iterable[tuple[str, str]],
) -> dict[str, object]:
    """Return exact Core/driver/scorer source identities for selected Cases."""

    normalized = tuple(sorted(set(bindings)))
    modules = set(_COMMON_SUITE_MODULES)
    for binding in normalized:
        try:
            modules.update(_CASE_IMPLEMENTATIONS[binding])
        except KeyError as exc:
            raise ValueError(
                f"evaluation implementation binding is not registered: {binding[0]}/{binding[1]}"
            ) from exc
    return {
        "catalog_version": IMPLEMENTATION_CATALOG_VERSION,
        "bindings": [
            {"plugin_id": plugin_id, "driver_id": driver_id}
            for plugin_id, driver_id in normalized
        ],
        "modules": {
            module_name: _suite_module_sha256(module_name)
            for module_name in sorted(modules)
        },
    }


def _suite_module_sha256(module_name: str) -> str:
    if module_name.startswith(_TRUSTED_NAMESPACE):
        return trusted_module_sha256(module_name)
    return trusted_runtime_module_sha256(module_name)


def comparison_implementation_snapshot() -> dict[str, object]:
    modules = sorted(set(_COMPARISON_IMPLEMENTATIONS))
    return {
        "catalog_version": IMPLEMENTATION_CATALOG_VERSION,
        "modules": {
            module_name: _suite_module_sha256(module_name)
            for module_name in modules
        },
    }


def runtime_implementation_snapshot(backend: str) -> dict[str, object]:
    """Return source identities for the exact trusted Agent runtime lane."""

    normalized = str(backend).strip().lower()
    try:
        backend_modules = _BACKEND_RUNTIME_IMPLEMENTATIONS[normalized]
    except KeyError as exc:
        raise ValueError(f"runtime implementation backend is unsupported: {backend!r}") from exc
    common_modules = (
        _COMMON_RUNTIME_IMPLEMENTATIONS
        if normalized in {"codex", "langgraph", "native"}
        else ()
    )
    modules = sorted(set(common_modules).union(backend_modules))
    return {
        "catalog_version": RUNTIME_IMPLEMENTATION_CATALOG_VERSION,
        "backend": normalized,
        "modules": {
            module_name: trusted_runtime_module_sha256(module_name)
            for module_name in modules
        },
    }


__all__ = [
    "IMPLEMENTATION_CATALOG_VERSION",
    "RUNTIME_IMPLEMENTATION_CATALOG_VERSION",
    "comparison_implementation_snapshot",
    "runtime_implementation_snapshot",
    "suite_implementation_snapshot",
    "trusted_module_sha256",
    "trusted_runtime_module_sha256",
]
