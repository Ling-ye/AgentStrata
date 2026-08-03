"""Immutable model-selection contracts shared across routing and job execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CODE_MODEL_LANE = "code"
MODEL_SELECTION_SCOPE_SESSION = "session"
MODEL_SELECTION_SCOPE_ONCE = "once"
MODEL_SELECTION_SCOPES = {
    MODEL_SELECTION_SCOPE_SESSION,
    MODEL_SELECTION_SCOPE_ONCE,
}
MODEL_SELECTION_SOURCE_DEFAULT = "default"
MODEL_SELECTION_SOURCE_PROFILE = "profile"
MODEL_SELECTION_SOURCES = {
    MODEL_SELECTION_SOURCE_DEFAULT,
    MODEL_SELECTION_SOURCE_PROFILE,
}
CODEX_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}


@dataclass(frozen=True)
class CodeModelProfile:
    """One BotSpec-allowlisted Codex model and reasoning combination."""

    model: str
    reasoning_effort: str = "medium"

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Codex model profile model must not be empty")
        if self.reasoning_effort not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                "unsupported Codex reasoning effort: "
                f"{self.reasoning_effort!r}"
            )

    def to_payload(self) -> dict[str, str]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class CodeModelSelection:
    """Frozen effective model selection attached to a queued Codex job."""

    provider: str
    model: str
    reasoning_effort: str
    scope: str = MODEL_SELECTION_SCOPE_SESSION
    source: str = MODEL_SELECTION_SOURCE_DEFAULT
    profile: str = ""
    lane: str = CODE_MODEL_LANE

    def __post_init__(self) -> None:
        if self.lane != CODE_MODEL_LANE:
            raise ValueError(f"unsupported model-selection lane: {self.lane!r}")
        if not self.provider.strip():
            raise ValueError("model-selection provider must not be empty")
        if not self.model.strip():
            raise ValueError("model-selection model must not be empty")
        if self.reasoning_effort not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                "unsupported Codex reasoning effort: "
                f"{self.reasoning_effort!r}"
            )
        if self.scope not in MODEL_SELECTION_SCOPES:
            raise ValueError(f"unsupported model-selection scope: {self.scope!r}")
        if self.source not in MODEL_SELECTION_SOURCES:
            raise ValueError(f"unsupported model-selection source: {self.source!r}")
        if self.source == MODEL_SELECTION_SOURCE_PROFILE and not self.profile.strip():
            raise ValueError("profile model selection requires a profile name")
        if self.source == MODEL_SELECTION_SOURCE_DEFAULT and self.profile:
            raise ValueError("default model selection cannot name a profile")

    def to_payload(self) -> dict[str, str]:
        return {
            "lane": self.lane,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "scope": self.scope,
            "source": self.source,
            "profile": self.profile,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "CodeModelSelection":
        if not isinstance(payload, dict):
            raise ValueError("model selection must be an object")
        return cls(
            lane=str(payload.get("lane") or CODE_MODEL_LANE).strip().lower(),
            provider=str(payload.get("provider") or "").strip().lower(),
            model=str(payload.get("model") or "").strip(),
            reasoning_effort=str(
                payload.get("reasoning_effort") or ""
            ).strip().lower(),
            scope=str(
                payload.get("scope") or MODEL_SELECTION_SCOPE_SESSION
            ).strip().lower(),
            source=str(
                payload.get("source") or MODEL_SELECTION_SOURCE_DEFAULT
            ).strip().lower(),
            profile=str(payload.get("profile") or "").strip().lower(),
        )


__all__ = [
    "CODE_MODEL_LANE",
    "CODEX_REASONING_EFFORTS",
    "CodeModelProfile",
    "CodeModelSelection",
    "MODEL_SELECTION_SCOPE_ONCE",
    "MODEL_SELECTION_SCOPE_SESSION",
    "MODEL_SELECTION_SCOPES",
    "MODEL_SELECTION_SOURCE_DEFAULT",
    "MODEL_SELECTION_SOURCE_PROFILE",
    "MODEL_SELECTION_SOURCES",
]
