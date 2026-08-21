"""Small contracts for trusted, host-owned persona control.

The main Agent never owns persona persistence. An interpreter may describe a
request, but only ``PersonaMutationReceipt.ok`` proves that protected state was
changed by the host.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


PersonaOperation = Literal[
    "none", "help", "show", "set", "append", "research", "refresh", "clear", "confirm", "cancel"
]
PersonaConfidence = Literal["high", "medium", "low"]
PersonaScope = Literal["default", "global", "group", "user"]


@dataclass(frozen=True)
class PersonaControlSpec:
    """One explicit BotSpec switch; search is an optional runtime enhancement."""

    enabled: bool = False


@dataclass(frozen=True)
class PersonaDirective:
    """A validated interpretation of one message.

    ``text`` and ``residual_text`` are exact substrings of the current message,
    except that structured slash-command arguments are exact command suffixes.
    The interpreter cannot supply paths, identities, or authority.
    """

    operation: PersonaOperation = "none"
    confidence: PersonaConfidence = "low"
    scope: PersonaScope = "default"
    text: str = ""
    residual_text: str = ""
    enrich: bool = False
    source: str = "none"
    reason: str = ""
    model: str = ""
    usage: Mapping[str, Any] | None = None

    @property
    def handles_turn(self) -> bool:
        return self.operation != "none"


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
    operation: Literal["set", "append", "research", "clear"]
    scope: str
    text: str
    content_sha256: str
    actor_id: str
    chat_id: str
    expires_at: float
    requires_research: bool = False


__all__ = [
    "PersonaConfidence",
    "PersonaControlSpec",
    "PersonaDirective",
    "PersonaDraftCall",
    "PersonaDraftResult",
    "PersonaMutationReceipt",
    "PersonaMutationRequest",
    "PersonaOperation",
    "PersonaScope",
    "PendingPersonaProposal",
]
