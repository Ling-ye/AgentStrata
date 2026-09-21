"""Resolve declared model fields without creating clients or reading ambient env."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from chatcopilot.contracts.model_runtime import ChatGPTAuthRef
from chatcopilot.core.config import LLMConfig, load_config


def resolve_model_config(
    declaration: Any, *, fallback: LLMConfig, prefix: str | None, environment: Mapping[str, str]
) -> LLMConfig:
    config = replace(fallback)
    inherited_prefix = getattr(declaration, "inherit_env_prefix", None)
    if inherited_prefix:
        config = load_config(env_prefix=inherited_prefix, environment=environment).llm
    for name in ("provider", "model", "api", "base_url", "reasoning_effort", "timeout"):
        value = getattr(declaration, name, None)
        if value is not None:
            setattr(config, name, value)
    auth = getattr(declaration, "auth", None)
    if auth is not None:
        config.auth_mode = auth.mode
        if isinstance(auth, ChatGPTAuthRef):
            config.auth_profile = auth.profile
            config.provider, config.api = "openai", "chatgpt_responses"
            config.base_url = "https://chatgpt.com/backend-api/codex"
            config.api_key = ""
        else:
            config.key_env = auth.key_env
            config.api_key = environment.get(auth.key_env, "")
            if config.provider == "openai" and getattr(declaration, "api", None) is None:
                config.api = "openai_responses"
                config.base_url = "https://api.openai.com/v1"
    if prefix:
        for name in ("model", "base_url", "reasoning_effort", "timeout"):
            value = environment.get(f"{prefix}_{name.upper()}")
            if value:
                setattr(config, name, int(value) if name == "timeout" else value)
        if auth is None and config.auth_mode == "api_key":
            if environment.get(f"{prefix}_API_KEY"):
                config.key_env = f"{prefix}_API_KEY"
                config.api_key = environment[config.key_env]
    config.credential_root = environment.get("CHATCOPILOT_CODEX_BOT_HOME", config.credential_root)
    config.model_route()
    return config
