"""Post-generation quality gate for agent responses.

Two levels:

- **Level 0** (``RegexGate``): zero-LLM-cost heuristic checks — untagged
  factual claims, suspicious fabricated URLs, self-contradictory "I don't know"
  followed by a definitive answer.  Always enabled; results are advisory
  metadata on ``AgentResult``.
- **Level 1** (``LlmCritiqueGate``): one extra LLM call to critique the draft
  response against accuracy rules.  Opt-in via
  ``{PREFIX}_QUALITY_GATE_LEVEL=1``.  When issues are found the critique is
  fed back as a user message for one revision pass (no tools).

Both levels implement :class:`QualityGate` so ``AgentSession`` only sees the
protocol.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol


_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """Outcome of a quality gate check."""

    ok: bool
    issues: tuple[str, ...] = ()
    level: int = 0
    decision_source: str = "script"
    gate_skipped: str = ""
    elapsed_ms: int = 0


class QualityGate(Protocol):
    """Checks ``final_text`` and returns structured issues."""

    def check(self, final_text: str) -> GateResult: ...


# ---------------------------------------------------------------------------
# Level 0 — regex / heuristic (zero LLM cost)
# ---------------------------------------------------------------------------

_FACT_PATTERN = re.compile(
    r"(?<!\[)"                # not already preceded by a tag bracket
    r"(?:"
    r"\b\d{4}\s*年"           # year mentions like "2024年"
    r"|\bv?\d+\.\d+"         # version numbers like "3.12" or "v2.1"
    r"|\b\d{1,3}(?:[,，]\d{3})+\b"  # large numbers like "1,000,000"
    r"|\b\d+(?:\.\d+)?%"     # percentages like "30%"
    r")"
)
_TAG_PATTERN = re.compile(r"\[(?:KNOWN|COMPUTED|INFERRED|COMMON|FRAME|GUESS)\]")

_SUSPICIOUS_URL = re.compile(
    r"https?://(?:"
    r"example\.(?:com|org|net)"
    r"|(?:www\.)?fake\w*\.com"
    r"|placeholder\.\w+"
    r")"
)

_DONT_KNOW_THEN_ANSWER = re.compile(
    r"I don'?t know\.?"
    r"[\s\S]{0,200}"
    r"(?:答案是|结论是|可以确定|实际上是|事实上)"
)


class RegexGate:
    """Level 0: pure heuristic checks, no LLM calls."""

    def check(self, final_text: str) -> GateResult:
        issues: list[str] = []

        fact_matches = list(_FACT_PATTERN.finditer(final_text))
        if fact_matches:
            for m in fact_matches[:3]:
                start = max(0, m.start() - 60)
                end = min(len(final_text), m.end() + 60)
                context = final_text[start:end]
                if not _TAG_PATTERN.search(context):
                    issues.append(
                        f"untagged_factual_claim near '{m.group()}'"
                    )

        for m in _SUSPICIOUS_URL.finditer(final_text):
            issues.append(f"suspicious_url: {m.group()}")

        if _DONT_KNOW_THEN_ANSWER.search(final_text):
            issues.append("contradiction: 'I don't know' followed by definitive answer")

        return GateResult(ok=not issues, issues=tuple(issues), level=0)


# ---------------------------------------------------------------------------
# Level 1 — LLM critique (one extra call, opt-in)
# ---------------------------------------------------------------------------

_CRITIQUE_PROMPT = """\
You are a quality reviewer. Check the assistant's draft response for these issues ONLY:
1. Factual claims missing [KNOWN]/[INFERRED]/[COMPUTED]/[GUESS] tags
2. Fabricated URLs, citations, or paper titles
3. Claims that should have been search-verified but were not

Respond with EXACTLY one JSON object: {"ok": true} if no issues, or {"ok": false, "issues": ["issue1", "issue2"]} if there are problems. No other text."""


class LlmCritiqueGate:
    """Level 1: one LLM call to critique the draft response."""

    def __init__(self, llm: object) -> None:
        self._llm = llm

    def check(self, final_text: str) -> GateResult:
        import json

        if len(final_text) < 20:
            return GateResult(
                ok=True,
                level=1,
                decision_source="script",
                gate_skipped="short_response",
            )

        started = time.monotonic()
        try:
            result = self._llm.chat(  # type: ignore[attr-defined]
                messages=[
                    {"role": "system", "content": _CRITIQUE_PROMPT},
                    {"role": "user", "content": f"Draft response to review:\n\n{final_text[:3000]}"},
                ],
                tools=[],
                stream=False,
            )
            content = (result.content or "").strip()
            data = json.loads(content)
            if isinstance(data, dict) and not data.get("ok", True):
                raw_issues = data.get("issues", [])
                issues = tuple(str(i) for i in raw_issues if isinstance(i, str))
                return GateResult(
                    ok=False,
                    issues=issues,
                    level=1,
                    decision_source="llm",
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = round((time.monotonic() - started) * 1000)
            reason = f"llm_error:{type(exc).__name__}"
            _LOGGER.warning(
                "quality gate skipped | gate_skipped=%s elapsed_ms=%d",
                reason,
                elapsed_ms,
            )
            return GateResult(
                ok=True,
                level=1,
                decision_source="llm",
                gate_skipped=reason,
                elapsed_ms=elapsed_ms,
            )

        return GateResult(
            ok=True,
            level=1,
            decision_source="llm",
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_quality_gate(*, level: int, llm: object | None = None) -> QualityGate | None:
    """Build a quality gate for the requested level.

    Returns ``None`` when ``level < 0`` (gate disabled).
    Level 0 returns a ``RegexGate`` (default, zero cost).
    Level 1 returns an ``LlmCritiqueGate`` that wraps a ``RegexGate`` — L0
    issues are always included.
    """
    if level < 0:
        return None
    if level == 0:
        return RegexGate()
    if level >= 1 and llm is not None:
        return _CompositeGate(RegexGate(), LlmCritiqueGate(llm))
    return RegexGate()


@dataclass(frozen=True)
class _CompositeGate:
    """Run L0 first; if L0 finds issues skip the L1 LLM call."""

    l0: RegexGate
    l1: LlmCritiqueGate

    def check(self, final_text: str) -> GateResult:
        r0 = self.l0.check(final_text)
        if not r0.ok:
            return r0
        return self.l1.check(final_text)


__all__ = [
    "GateResult",
    "LlmCritiqueGate",
    "QualityGate",
    "RegexGate",
    "build_quality_gate",
]
