"""Static execution/scoring implementation identity for Evaluation fingerprints."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Iterable


IMPLEMENTATION_CATALOG_VERSION = "agentstrata-eval-implementation/v1"
RUNTIME_IMPLEMENTATION_CATALOG_VERSION = "agentstrata-runtime-implementation/v1"
_TRUSTED_NAMESPACE = "chatcopilot.evals."
_TRUSTED_PACKAGE_NAMESPACE = "chatcopilot."
_MODULE_PART_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_EVALS_ROOT = Path(__file__).absolute().parent
_PACKAGE_ROOT = _EVALS_ROOT.parent
_MAX_SOURCE_BYTES = 4 * 1024 * 1024

_COMMON_SUITE_MODULES = (
    "chatcopilot.evals.evaluations",
    "chatcopilot.evals.judges",
    "chatcopilot.evals.runner",
)
_CASE_IMPLEMENTATIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("generic-agent", "agent_isolated"): (
        "chatcopilot.evals.capability_executor",
        "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.isolated_executor",
    ),
    ("generic-agent", "agent_configured"): (
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
        "chatcopilot.core.ingress_receipts",
        "chatcopilot.core.persona_control",
        "chatcopilot.core.persistent_state",
        "chatcopilot.core.session_env_store",
        "chatcopilot.middleware.acp.access_gate",
        "chatcopilot.middleware.acp.agent_bridge",
        "chatcopilot.middleware.acp.event_translator",
        "chatcopilot.platforms.qq.ingress_probe",
        "chatcopilot.platforms.qq.access_proxy",
        "chatcopilot.middleware.acp.group_conversation",
        "chatcopilot.middleware.acp.persona_control",
        "chatcopilot.middleware.acp.server",
        "chatcopilot.middleware.acp.transport_attestation",
        "chatcopilot.middleware.acp.turn_orchestrator",
        "chatcopilot.middleware.acp.workspace_service",
        "chatcopilot.middleware.runtime.tasks",
    ),
    ("gaia", "agent_configured"): (
        "chatcopilot.evals.adapters.gaia",
        "chatcopilot.evals.judges_llm",
    ),
    ("ifeval", "agent_configured"): ("chatcopilot.evals.adapters.ifeval",),
    ("bfcl", "direct_llm"): ("chatcopilot.evals.adapters.bfcl",),
}
_COMPARISON_IMPLEMENTATIONS = (
    "chatcopilot.evals.adapters.gaia",
    "chatcopilot.evals.adapters.ifeval",
    "chatcopilot.evals.isolated_executor",
    "chatcopilot.evals.profiles",
    "chatcopilot.evals.runner",
)
_COMMON_RUNTIME_IMPLEMENTATIONS = (
    "chatcopilot.agent.backends.registry",
    "chatcopilot.agent.runtime",
    "chatcopilot.agent.search.coordinator",
    "chatcopilot.agent.search.providers",
    "chatcopilot.agent.search.router",
    "chatcopilot.agent.subagents.registry",
    "chatcopilot.agent.subagents.result",
    "chatcopilot.agent.subagents.runner",
    "chatcopilot.agent.tools.executor",
    "chatcopilot.agent.tools.registry",
    "chatcopilot.agent.turn",
    "chatcopilot.middleware.acp.access_gate",
    "chatcopilot.middleware.acp.agent_bridge",
    "chatcopilot.platforms.qq.at_proxy",
)
_BACKEND_RUNTIME_IMPLEMENTATIONS: dict[str, tuple[str, ...]] = {
    "codex": (
        "chatcopilot.agent.backends.codex",
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
    relative = Path(*relative_parts).with_suffix(".py")
    path = root / relative
    try:
        root_info = root.stat(follow_symlinks=False)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"{scope} root is unsafe")
        parent_snapshots: list[tuple[Path, os.stat_result]] = [(root, root_info)]
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            ancestor = current.stat(follow_symlinks=False)
            if stat.S_ISLNK(ancestor.st_mode) or not stat.S_ISDIR(ancestor.st_mode):
                raise ValueError(f"{scope} parent is unsafe: {module_name}")
            parent_snapshots.append((current, ancestor))
        info = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{scope} source is a symlink: {module_name}")
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{scope} source is unavailable: {module_name}") from exc
    if resolved_root not in resolved.parents:
        raise ValueError(f"{scope} is outside trusted package: {module_name}")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"{scope} inode is unsafe: {module_name}")
    if info.st_size > _MAX_SOURCE_BYTES:
        raise ValueError(f"{scope} source is too large: {module_name}")

    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size != info.st_size
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise ValueError(f"{scope} changed before reading: {module_name}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_SOURCE_BYTES:
                raise ValueError(f"{scope} source is too large: {module_name}")
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if (
            finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
            or total != opened.st_size
        ):
            raise ValueError(f"{scope} changed while reading: {module_name}")
        after = path.stat(follow_symlinks=False)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
        ):
            raise ValueError(f"{scope} changed after reading: {module_name}")
        for parent, before in parent_snapshots:
            parent_after = parent.stat(follow_symlinks=False)
            if (
                parent_after.st_dev != before.st_dev
                or parent_after.st_ino != before.st_ino
                or parent_after.st_mtime_ns != before.st_mtime_ns
                or parent_after.st_ctime_ns != before.st_ctime_ns
                or stat.S_ISLNK(parent_after.st_mode)
                or not stat.S_ISDIR(parent_after.st_mode)
            ):
                raise ValueError(
                    f"{scope} parent changed while reading: {module_name}"
                )
        return digest.hexdigest()
    except OSError as exc:
        raise ValueError(f"{scope} cannot be read: {module_name}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
            module_name: trusted_module_sha256(module_name)
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
