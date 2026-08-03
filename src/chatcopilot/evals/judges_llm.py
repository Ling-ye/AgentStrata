"""LLM-as-Judge: use the same model to score responses when deterministic
matching fails.

Currently used as a fallback for GAIA: when normalized exact match fails,
the LLM is asked whether the response semantically matches the expected
answer. This avoids false negatives on open-ended answers like
"Blue Whale" vs "blue whale, the largest animal".

The judge prompt deliberately withholds ``must_have`` / ``must_not`` from
the model to prevent shortcut copying.  Only the expected answer and the
rubric (if present) anchor the evaluation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from chatcopilot.core.config import ChatConfig
from chatcopilot.evals.models import EvalCase, JudgeResult

log = logging.getLogger(__name__)

_JUDGE_SYSTEM = (
    "You are a strict evaluation judge. Given a question, an expected answer, "
    "and a model response, determine whether the response contains the correct "
    "answer. Respond ONLY with a JSON object: "
    '{"match": true/false, "reasoning": "<one sentence>"}'
)

_JUDGE_USER_TEMPLATE = """Question: {question}
Expected answer: {expected}
Model response: {response}

Does the model response contain the correct answer (semantically equivalent)?
Respond with JSON only."""


def judge_llm_rubric(
    case: EvalCase,
    final_text: str,
    chat_config: ChatConfig,
) -> JudgeResult:
    """Score a GAIA-style case using the same LLM as a semantic match judge.

    Only called when deterministic exact match already failed.
    """

    expected = str(case.metadata.get("answer", "")).strip()
    if not expected:
        return JudgeResult(
            score=0.0,
            max_score=1.0,
            passed=False,
            reasons=("no expected answer for LLM judge",),
            missing=("metadata.answer",),
        )

    try:
        from chatcopilot.core.llm_client import LLMClient

        llm = LLMClient(chat_config.llm)
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {
                "role": "user",
                "content": _JUDGE_USER_TEMPLATE.format(
                    question=case.input[:500],
                    expected=expected[:200],
                    response=final_text[:1000],
                ),
            },
        ]
        result = llm.chat(messages=messages, tools=None, stream=False)
        verdict = _parse_verdict(result.content or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM judge failed: %s", exc)
        return JudgeResult(
            score=0.0,
            max_score=1.0,
            passed=False,
            reasons=(f"llm_judge_error: {exc}",),
        )

    return JudgeResult(
        score=1.0 if verdict["match"] else 0.0,
        max_score=1.0,
        passed=verdict["match"],
        reasons=(f"llm_judge: {verdict['reasoning']}",),
    )


def _parse_verdict(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {
                "match": bool(data.get("match", False)),
                "reasoning": str(data.get("reasoning", "")),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    text_lower = text.lower()
    if '"match": true' in text_lower or '"match":true' in text_lower:
        return {"match": True, "reasoning": "parsed from partial JSON"}
    return {"match": False, "reasoning": "could not parse LLM judge output"}


__all__ = ["judge_llm_rubric"]
