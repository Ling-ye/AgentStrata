"""Shared binding resolution and client construction for all runtime hosts."""
from __future__ import annotations
from typing import Any, Mapping
from chatcopilot.core.config import LLMConfig
from chatcopilot.core.model_settings import resolve_binding


def resolve_model_config(declaration: Any, *, fallback: LLMConfig, prefix: str | None,
                         environment: Mapping[str, str], document: dict | None = None) -> LLMConfig:
    return resolve_binding(declaration.binding, environment=environment, document=document)


def create_model_client(config: LLMConfig):
    from chatcopilot.core.llm_client import LLMClient
    config.model_route()
    return LLMClient(config)
