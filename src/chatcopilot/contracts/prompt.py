"""Provider-neutral prompt plan contracts.

Prompt policy is assembled once into an immutable plan.  Backends may render
the plan for their transport, but may not append policy of their own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal


PromptKind = Literal[
    "runtime_policy",
    "bot_identity",
    "capability_policy",
    "skill_instruction",
    "response_style",
    "dynamic_persona",
    "untrusted_context",
    "session_fact",
    "user_input",
]
PromptTrust = Literal[
    "trusted_policy",
    "trusted_runtime_fact",
    "bot_instruction",
    "untrusted_data",
]
PromptCacheScope = Literal["global", "bot", "session", "turn"]

_KIND_TRUST = {
    "runtime_policy": "trusted_policy",
    "capability_policy": "trusted_policy",
    "session_fact": "trusted_runtime_fact",
    "bot_identity": "bot_instruction",
    "skill_instruction": "bot_instruction",
    "response_style": "bot_instruction",
    "dynamic_persona": "untrusted_data",
    "untrusted_context": "untrusted_data",
    "user_input": "untrusted_data",
}
_KINDS = frozenset(_KIND_TRUST)
_TRUST = frozenset(_KIND_TRUST.values())
_CACHE_SCOPES = frozenset({"global", "bot", "session", "turn"})
_BACKENDS = frozenset({"native", "langgraph", "codex"})
_ROLES = frozenset({"owner", "admin", "user"})
_CHANNELS = frozenset({"private", "group"})


@dataclass(frozen=True)
class PromptLayer:
    id: str
    kind: PromptKind
    trust: PromptTrust
    cache_scope: PromptCacheScope
    content: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        layer_id = self.id.strip()
        content = self.content.strip()
        if not layer_id:
            raise ValueError("prompt layer id cannot be empty")
        if self.kind not in _KINDS:
            raise ValueError(f"unknown prompt layer kind: {self.kind!r}")
        if self.trust not in _TRUST:
            raise ValueError(f"unknown prompt layer trust: {self.trust!r}")
        if self.cache_scope not in _CACHE_SCOPES:
            raise ValueError(f"unknown prompt cache scope: {self.cache_scope!r}")
        if not content:
            raise ValueError(f"prompt layer {layer_id!r} cannot be empty")
        expected_trust = _KIND_TRUST[self.kind]
        if self.trust != expected_trust:
            raise ValueError(f"{self.kind} layers must use {expected_trust}, got {self.trust}")
        object.__setattr__(self, "id", layer_id)
        object.__setattr__(self, "content", content)
        object.__setattr__(
            self,
            "content_sha256",
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True)
class PromptPlan:
    layers: tuple[PromptLayer, ...]
    effective_backend: str
    effective_model: str | None
    role: str
    channel_kind: str
    tool_projection_digest: str = ""
    estimated_tokens: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("prompt plan schema_version must be 1")
        if self.effective_backend not in _BACKENDS:
            raise ValueError(f"unsupported prompt backend: {self.effective_backend!r}")
        if self.role not in _ROLES:
            raise ValueError(f"unsupported prompt role: {self.role!r}")
        if self.channel_kind not in _CHANNELS:
            raise ValueError(f"unsupported prompt channel: {self.channel_kind!r}")
        ids = tuple(layer.id for layer in self.layers)
        if len(ids) != len(set(ids)):
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            raise ValueError("duplicate prompt layer ids: " + ", ".join(duplicates))
        if self.estimated_tokens < 0:
            raise ValueError("estimated_tokens cannot be negative")


@dataclass(frozen=True)
class PromptRenderReceipt:
    layer_ids: tuple[str, ...]
    layer_hashes: tuple[str, ...]
    rendered_sha256: str
    prompt_chars: int
    tool_schema_chars: int
    estimated_tokens: int
    partition_hashes: tuple[tuple[PromptTrust, str], ...] = ()


@dataclass(frozen=True)
class BotPromptProfile:
    identity: str
    response_style: str
    refusal_style: str = ""
    role_styles: dict[str, str] = field(default_factory=dict)
    mode_styles: dict[str, str] = field(default_factory=dict)


__all__ = [
    "BotPromptProfile",
    "PromptCacheScope",
    "PromptKind",
    "PromptLayer",
    "PromptPlan",
    "PromptRenderReceipt",
    "PromptTrust",
]
