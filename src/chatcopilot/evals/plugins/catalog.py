"""Static allowlist and loader for repository-owned evaluation plugins."""

from __future__ import annotations

import hashlib
import importlib
import os
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from chatcopilot.evals.plugins.base import (
    EvaluationPlugin,
    PLUGIN_API_VERSION,
    STATIC_PLUGIN_BINDING_VERSION,
)

_TRUSTED_PREFIX = "chatcopilot.evals.plugins."


@dataclass(frozen=True)
class PluginBinding:
    plugin_id: str
    module: str
    api_version: str
    allowed_drivers: frozenset[str]
    binding_version: str = STATIC_PLUGIN_BINDING_VERSION


_BINDINGS: tuple[PluginBinding, ...] = (
    PluginBinding(
        "gaia",
        f"{_TRUSTED_PREFIX}gaia",
        PLUGIN_API_VERSION,
        frozenset({"agent_configured", "dry_run"}),
    ),
    PluginBinding(
        "bfcl", f"{_TRUSTED_PREFIX}bfcl", PLUGIN_API_VERSION, frozenset({"direct_llm", "dry_run"})
    ),
    PluginBinding(
        "ifeval",
        f"{_TRUSTED_PREFIX}ifeval",
        PLUGIN_API_VERSION,
        frozenset({"agent_configured", "dry_run"}),
    ),
    PluginBinding(
        "generic-agent",
        f"{_TRUSTED_PREFIX}generic_agent",
        PLUGIN_API_VERSION,
        frozenset({"agent_isolated", "agent_configured", "dry_run"}),
    ),
    PluginBinding(
        "acp-scenario",
        f"{_TRUSTED_PREFIX}acp_scenario",
        PLUGIN_API_VERSION,
        frozenset({"acp_scenario", "dry_run"}),
    ),
    PluginBinding(
        "qq-live",
        f"{_TRUSTED_PREFIX}qq_live",
        PLUGIN_API_VERSION,
        frozenset({"qq_live", "dry_run"}),
    ),
)
_BY_ID = {binding.plugin_id: binding for binding in _BINDINGS}


def list_plugin_bindings() -> tuple[PluginBinding, ...]:
    return _BINDINGS


def get_plugin_binding(plugin_id: str) -> PluginBinding:
    normalized = plugin_id.strip().lower().replace("_", "-")
    try:
        return _BY_ID[normalized]
    except KeyError as exc:
        raise ValueError(f"untrusted evaluation plugin id: {plugin_id}") from exc


@lru_cache(maxsize=None)
def get_evaluation_plugin(plugin_id: str) -> EvaluationPlugin:
    """Load and validate only an exact entry from the static binding catalog."""

    binding = get_plugin_binding(plugin_id)
    return load_plugin_binding(binding)


def load_plugin_binding(binding: PluginBinding) -> EvaluationPlugin:
    """Load one explicit binding; exposed for catalog validation tests."""

    if binding not in _BINDINGS:
        raise ValueError(
            f"evaluation plugin binding is not in the static catalog: {binding.plugin_id}"
        )
    if not binding.module.startswith(_TRUSTED_PREFIX):
        raise ValueError(f"evaluation plugin module is outside trusted namespace: {binding.module}")
    module = importlib.import_module(binding.module)
    plugin = getattr(module, "PLUGIN", None)
    if not isinstance(plugin, EvaluationPlugin):
        raise TypeError(f"{binding.module}.PLUGIN must be EvaluationPlugin")
    if plugin.plugin_id != binding.plugin_id:
        raise ValueError(f"evaluation plugin id mismatch for {binding.module}")
    if plugin.api_version != binding.api_version or plugin.api_version != PLUGIN_API_VERSION:
        raise ValueError(f"evaluation plugin API mismatch for {binding.plugin_id}")
    if plugin.implementation_module != binding.module:
        raise ValueError(
            f"evaluation plugin implementation module mismatch for {binding.plugin_id}"
        )
    if plugin.allowed_drivers != binding.allowed_drivers:
        raise ValueError(f"evaluation plugin driver allowlist mismatch for {binding.plugin_id}")
    if not callable(plugin.load_cases):
        raise TypeError(f"evaluation plugin load_cases hook is not callable: {binding.plugin_id}")
    for hook_name in ("preflight", "prepare", "build_task", "execute_trial", "judge", "cleanup"):
        hook = getattr(plugin, hook_name)
        if hook is not None and not callable(hook):
            raise TypeError(
                f"evaluation plugin {hook_name} hook is not callable: {binding.plugin_id}"
            )
    return plugin


def plugin_implementation_sha256(plugin_id: str) -> str:
    """Hash the exact trusted plugin source file, failing closed on ambiguity."""

    binding = get_plugin_binding(plugin_id)
    if binding not in _BINDINGS:
        raise ValueError(f"evaluation plugin binding is not static: {binding.plugin_id}")
    plugin = get_evaluation_plugin(binding.plugin_id)
    if plugin.implementation_module != binding.module:
        raise ValueError(f"evaluation plugin implementation drift: {binding.plugin_id}")
    module = importlib.import_module(binding.module)
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"evaluation plugin source is unavailable: {binding.plugin_id}")
    path = Path(raw_path)
    if not path.is_absolute() or path.suffix != ".py" or path.is_symlink():
        raise ValueError(f"evaluation plugin source is not a trusted .py file: {binding.plugin_id}")
    try:
        resolved = path.resolve(strict=True)
        trusted_root = Path(__file__).resolve(strict=True).parent
        info = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"evaluation plugin source is unavailable: {binding.plugin_id}") from exc
    if resolved.parent != trusted_root or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(
            f"evaluation plugin source is outside the trusted package: {binding.plugin_id}"
        )
    if info.st_size > 2 * 1024 * 1024:
        raise ValueError(f"evaluation plugin source is too large: {binding.plugin_id}")
    descriptor = -1
    try:
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size != info.st_size
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise ValueError(
                f"evaluation plugin source changed before reading: {binding.plugin_id}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        if (
            finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ValueError(f"evaluation plugin source changed while reading: {binding.plugin_id}")
        payload = b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"evaluation plugin source cannot be read: {binding.plugin_id}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != info.st_size:
        raise ValueError(f"evaluation plugin source changed while reading: {binding.plugin_id}")
    return hashlib.sha256(payload).hexdigest()


def plugin_binding_snapshot(plugin_id: str) -> dict[str, object]:
    """Return canonical static binding identity plus trusted source digest."""

    binding = get_plugin_binding(plugin_id)
    if binding not in _BINDINGS:
        raise ValueError(f"evaluation plugin binding is not static: {binding.plugin_id}")
    return {
        "plugin_id": binding.plugin_id,
        "api_version": binding.api_version,
        "binding_version": binding.binding_version,
        "implementation_module": binding.module,
        "implementation_sha256": plugin_implementation_sha256(binding.plugin_id),
        "allowed_drivers": sorted(binding.allowed_drivers),
    }


__all__ = [
    "PluginBinding",
    "get_evaluation_plugin",
    "get_plugin_binding",
    "list_plugin_bindings",
    "load_plugin_binding",
    "plugin_binding_snapshot",
    "plugin_implementation_sha256",
]
