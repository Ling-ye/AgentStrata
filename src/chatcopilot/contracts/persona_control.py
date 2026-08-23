"""Contracts for trusted persona drafting, proposals, and persistence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class PersonaMutationRequest:
    operation: Literal["set", "clear"]
    scope: str
    text: str = ""
    confirm: bool = False


@dataclass(frozen=True)
class PersonaMutationReceipt:
    ok: bool
    operation: str
    scope: str
    content_sha256: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class PersonaDraftCall:
    """One explicit provider call made by the persona draft Agent."""

    model: str
    iteration: int
    ok: bool
    finish_reason: str = ""
    usage: Mapping[str, Any] | None = None
    elapsed_ms: int = 0
    error_code: str = ""
    error_kind: str = ""


@dataclass(frozen=True)
class PersonaDraftResult:
    """A complete Agent-authored persona document, or a bounded diagnostic."""

    markdown: str = ""
    source_urls: tuple[str, ...] = ()
    observed_source_urls: tuple[str, ...] = ()
    model: str = ""
    calls: tuple[PersonaDraftCall, ...] = ()
    search_calls: int = 0
    elapsed_ms: int = 0
    error_code: str = ""
    error_kind: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.markdown) and not self.error_code

    @property
    def usage(self) -> Mapping[str, int]:
        totals: dict[str, int] = {}
        for call in self.calls:
            for key, value in (call.usage or {}).items():
                if isinstance(value, (int, float)) and value >= 0:
                    totals[str(key)] = totals.get(str(key), 0) + int(value)
        return totals


@dataclass(frozen=True)
class PendingPersonaProposal:
    operation: Literal["set", "append", "research", "refresh", "clear"]
    scope: str
    text: str
    # Bind confirmation to the protected scope snapshot observed when proposed.
    content_sha256: str
    actor_id: str
    chat_id: str
    expires_at: float
    requires_research: bool = False


__all__ = [
    "PersonaDraftCall",
    "PersonaDraftResult",
    "PersonaMutationReceipt",
    "PersonaMutationRequest",
    "PendingPersonaProposal",
]
