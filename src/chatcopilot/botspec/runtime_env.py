"""Runtime environment derived from a loaded BotSpec.

The process ``HOME`` may be changed by cc-connect to an isolated runtime
directory, so codebase paths must not depend on ``~`` expansion at tool-call
time. This module centralizes the env values that need to be stable for both
the main ACP process and background workers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from chatcopilot.botspec.model import BotSpec, LLMSpec
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.core.config import LLMConfig, load_llm_profile
from chatcopilot.project import ENV_PREFIX

_SOURCE_ROOT_ENV = f"{ENV_PREFIX}_SOURCE_ROOT"
_SOURCE_BOT_SPEC_ENV = f"{ENV_PREFIX}_SOURCE_BOT_SPEC"
_RUNTIME_ROOT_ENV = f"{ENV_PREFIX}_RUNTIME_ROOT"
_CHATCOPILOT_CODEBASE_ROOT_ENV = f"{ENV_PREFIX}_CODEBASE_CHATCOPILOT_ROOT"
_ROOT_MARKERS = ("pyproject.toml", "AGENTS.md", ".git")


def apply_runtime_env(runtime: BotRuntimeContext) -> None:
    """Apply process env values resolved from the BotSpec runtime context."""

    os.environ.setdefault(f"{ENV_PREFIX}_BOT_ID", runtime.bot_id)
    os.environ.setdefault(f"{ENV_PREFIX}_INSTANCE_ID", runtime.instance_id)
    os.environ.setdefault(f"{ENV_PREFIX}_DISPLAY_NAME", runtime.display_name)
    if runtime.workspace_root:
        os.environ.setdefault(f"{ENV_PREFIX}_WORKSPACE_ROOT", runtime.workspace_root)
    if runtime.log_dir:
        os.environ.setdefault(f"{ENV_PREFIX}_LOG_DIR", runtime.log_dir)

    source_path = _source_path(runtime)
    source_root = _source_root(source_path)
    runtime_root = _runtime_root(runtime)
    os.environ.setdefault(_SOURCE_ROOT_ENV, str(source_root))
    os.environ.setdefault(_RUNTIME_ROOT_ENV, str(runtime_root))
    os.environ.setdefault(_CHATCOPILOT_CODEBASE_ROOT_ENV, str(source_root))

    codebase_registry = runtime.spec.resolve_path(runtime.spec.context.codebases.registry)
    if codebase_registry is not None:
        os.environ[f"{ENV_PREFIX}_CODEBASE_REGISTRY"] = str(codebase_registry.resolve())
        _reset_codebase_registry_cache()

    os.environ.update(resolve_runtime_environment(runtime.spec, os.environ, source_root=source_root))
    _reset_dev_config_cache()


def llm_runtime_env_defaults(llm: LLMSpec) -> dict[str, str]:
    """Translate versioned BotSpec LLM policy into legacy runtime env keys."""

    prefix = llm.env_prefix
    code = getattr(llm, "code", None)
    if code is None:
        return {}
    values = {
        f"{prefix}_ROUTER_ENABLED": "false",
        f"{prefix}_ROUTER_MODE": code.mode,
        f"{prefix}_ROUTER_CODE_PREFIXES": ",".join(code.prefixes),
        f"{prefix}_ROUTER_CHAT_PREFIXES": ",".join(code.chat_prefixes),
        f"{prefix}_RESEARCH_EXECUTION": llm.research_execution,
        f"{prefix}_RESEARCH_PREFIXES": ",".join(llm.research_prefixes),
        f"{prefix}_RESEARCH_WEB_SEARCH": llm.research_web_search,
        f"{prefix}_CODE_PROVIDER": code.provider,
        f"{prefix}_CODE_MODEL": code.model,
        f"{prefix}_CODE_REASONING_EFFORT": code.reasoning_effort,
        f"{prefix}_CODE_PROFILES_JSON": json.dumps(
            {
                name: profile.to_payload()
                for name, profile in sorted(code.profiles.items())
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        f"{prefix}_CODE_COMMAND": code.command,
        f"{prefix}_CODE_WORKDIR_ENV": code.workdir_env,
        f"{prefix}_CODE_TIMEOUT_SECONDS": str(code.timeout_seconds),
        f"{prefix}_CODE_ALLOWED_ROLES": ",".join(code.allowed_roles),
    }
    if code.code_task_profile:
        values[f"{prefix}_CODE_TASK_PROFILE"] = code.code_task_profile
    return values


def load_research_llm_config(
    llm: LLMSpec,
    *,
    fallback: LLMConfig,
    environment: Mapping[str, str] | None = None,
) -> LLMConfig:
    """Resolve the versioned research model default, then apply machine overrides."""

    configured = LLMConfig(
        base_url=fallback.base_url,
        model=getattr(llm, "research_model", None) or fallback.model,
        api_key=fallback.api_key,
        timeout=fallback.timeout,
    )
    prefix = getattr(llm, "research_env_prefix", None)
    return (
        load_llm_profile(prefix, fallback=configured, environment=environment)
        if prefix else configured
    )


def _source_root(source_path: Path) -> Path:
    start = source_path.resolve()
    current = start.parent if start.is_file() else start
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate
    return current


def _source_path(runtime: BotRuntimeContext) -> Path:
    source_override = os.environ.get(_SOURCE_BOT_SPEC_ENV, "").strip()
    if source_override:
        candidate = Path(source_override).expanduser()
        if candidate.exists():
            return candidate
    return runtime.source_path


def _runtime_root(runtime: BotRuntimeContext) -> Path:
    home = os.environ.get(f"{ENV_PREFIX}_HOME", "").strip()
    if home:
        candidate = Path(home).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    return _source_root(runtime.source_path)


def resolve_runtime_environment(spec: BotSpec, environment: Mapping[str, str], *, source_root: Path) -> dict[str, str]:
    """Resolve startup defaults without changing process environment or tool caches."""
    values = dict(environment)
    for key, value in llm_runtime_env_defaults(spec.llm).items():
        values.setdefault(key, value)
    values.setdefault(_SOURCE_ROOT_ENV, str(source_root))
    values.setdefault(_CHATCOPILOT_CODEBASE_ROOT_ENV, str(source_root))
    registry = spec.resolve_path(spec.context.codebases.registry)
    if registry is not None:
        values[f"{ENV_PREFIX}_CODEBASE_REGISTRY"] = str(registry.resolve())
    dev = spec.context.dev
    configured_root = values.get(dev.root_env, "").strip()
    if configured_root:
        values[f"{ENV_PREFIX}_DEV_ROOT"] = configured_root
    else:
        values.setdefault(f"{ENV_PREFIX}_DEV_ROOT", str(source_root))
    if dev.allowed_paths:
        values.setdefault(f"{ENV_PREFIX}_DEV_ALLOWED_PATHS", ",".join(dev.allowed_paths))
    if dev.denied_paths:
        values.setdefault(f"{ENV_PREFIX}_DEV_DENIED_PATHS", ",".join(dev.denied_paths))
    if dev.shell.timeout_max != 300:
        values.setdefault(f"{ENV_PREFIX}_DEV_SHELL_TIMEOUT_MAX", str(dev.shell.timeout_max))
    wiki = spec.context.wiki
    if wiki.enabled:
        configured_root = values.get(wiki.root_env, "").strip()
        if configured_root:
            values[f"{ENV_PREFIX}_WIKI_ROOT"] = configured_root
        values[f"{ENV_PREFIX}_WIKI_MAX_CHUNK_CHARS"] = str(wiki.max_chunk_chars)
    return values


def _reset_codebase_registry_cache() -> None:
    try:
        from chatcopilot.external_tools.codebase.config import reset_cache

        reset_cache()
    except Exception:  # noqa: BLE001 - env setup must not fail before tools are used.
        pass


def _reset_dev_config_cache() -> None:
    try:
        from chatcopilot.external_tools.dev.config import reset_cache

        reset_cache()
    except Exception:  # noqa: BLE001 - env setup must not fail before tools are used.
        pass


__all__ = ["apply_runtime_env", "llm_runtime_env_defaults"]
