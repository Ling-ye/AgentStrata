"""Deterministic result cleanup with optional semantic LLM synthesis.

Triggered after multi-step research when depth != "quick" and there are
at least two successful results.  A single tool-free LLM call scores
relevance, merges duplicate URLs, and returns a ranked findings list.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from chatcopilot.core.llm_client import LLMClient

_LOGGER = logging.getLogger(__name__)
_MIN_RESULTS_FOR_RERANK = 2
_MAX_INPUT_CHARS = 24000
_ITEM_KEYS = ("items", "findings", "results", "evidence", "organic_results")
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_SOURCE_WEIGHTS = {
    "github": 60,
    "tavily": 50,
    "brave": 45,
    "searxng": 40,
    "xiaohongshu": 30,
    "taoke": 20,
}

_SYSTEM_PROMPT = """\
You are a search-result analyst.  Given a research objective and raw results
from multiple sources, produce exactly one JSON object (no prose).

The input has already been mechanically URL/title deduplicated and stably
ordered. Perform only semantic work:
1. Merge findings that express the same fact across different sources.
2. Identify conflicts and rank findings by relevance to the objective.
3. Extract key findings as concise bullet-style strings.

Schema:
{
  "ranked_findings": [
    {
      "fact": "one-sentence finding",
      "source_url": "https://...",
      "source_name": "human-readable source",
      "confidence": "high|medium|low",
      "original_index": 0
    }
  ],
  "duplicates_merged": 0,
  "overall_confidence": "high|medium|low",
  "gaps": "what the results did NOT answer (empty string if fully answered)"
}

Rules:
- Return at most 10 ranked_findings.
- confidence reflects how well the finding is supported by the source.
- overall_confidence reflects how well the combined results answer the objective.
- Do NOT fabricate facts not present in the input.
- Keep source_url from the original results when available.
"""


class ResultReranker:
    """Stateless reranker backed by a lightweight LLM call."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def should_rerank(
        self,
        depth: str,
        ok_results: Sequence[dict[str, Any]],
    ) -> bool:
        if depth != "thorough":
            return False
        sources = {
            str(item.get("logical_source") or item.get("actual_source") or "").split(":", 1)[0]
            for item in ok_results
        }
        sources.discard("")
        return len(ok_results) >= _MIN_RESULTS_FOR_RERANK and len(sources) >= 2

    def prepare(
        self,
        results: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return prepare_results(results)

    def rerank(
        self,
        objective: str,
        results: list[dict[str, Any]],
        *,
        timeout: int = 15,
    ) -> dict[str, Any] | None:
        """Return reranked findings dict, or *None* on failure."""
        prepared, preprocessing = prepare_results(results)
        ok_results = [r for r in prepared if r.get("ok")]
        if len(ok_results) < _MIN_RESULTS_FOR_RERANK:
            return None

        payload = json.dumps(
            {"objective": objective, "results": ok_results},
            ensure_ascii=False,
        )
        if len(payload) > _MAX_INPUT_CHARS:
            payload = payload[:_MAX_INPUT_CHARS] + "\n[truncated]"

        try:
            response = self._llm.chat(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
                tools=None,
                stream=False,
                max_retries=0,
                timeout=max(1, timeout),
            )
            raw = json.loads(_extract_json(response.content))
            validated = _validate(raw)
            if validated is not None:
                validated["decision_source"] = "llm"
                validated["decision_reason"] = "thorough multi-source semantic merge"
                validated["preprocessing"] = preprocessing
            return validated
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("reranker failed, returning raw results: %s", exc)
            return None


def prepare_results(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deduplicate and stably rank nested search items without an LLM."""
    prepared = copy.deepcopy(results)
    candidates: list[tuple[tuple[int, float, int], str, int, str, int]] = []
    input_items = 0
    serial = 0
    for result_index, result in enumerate(prepared):
        summary = result.get("summary")
        if not isinstance(summary, dict):
            continue
        source = str(result.get("actual_source") or result.get("logical_source") or "")
        for item_key in _ITEM_KEYS:
            items = summary.get(item_key)
            if not isinstance(items, list):
                continue
            input_items += len(items)
            for item_index, item in enumerate(items):
                serial += 1
                identity = _item_identity(item) or f"unique:{serial}"
                candidates.append(
                    (
                        _item_score(item, source=source, serial=serial),
                        identity,
                        result_index,
                        item_key,
                        item_index,
                    )
                )

    winner: dict[str, tuple[int, str, int]] = {}
    for score, identity, result_index, item_key, item_index in candidates:
        current = winner.get(identity)
        if current is None:
            winner[identity] = (result_index, item_key, item_index)
            continue
        current_candidate = next(
            candidate
            for candidate in candidates
            if candidate[2:] == current
        )
        if score > current_candidate[0]:
            winner[identity] = (result_index, item_key, item_index)

    keep = set(winner.values())
    output_items = 0
    for result_index, result in enumerate(prepared):
        summary = result.get("summary")
        if not isinstance(summary, dict):
            continue
        source = str(result.get("actual_source") or result.get("logical_source") or "")
        for item_key in _ITEM_KEYS:
            items = summary.get(item_key)
            if not isinstance(items, list):
                continue
            selected = [
                (item_index, item)
                for item_index, item in enumerate(items)
                if (result_index, item_key, item_index) in keep
            ]
            selected.sort(
                key=lambda pair: _item_score(pair[1], source=source, serial=pair[0]),
                reverse=True,
            )
            summary[item_key] = [item for _, item in selected]
            output_items += len(selected)

    return prepared, {
        "decision_source": "script",
        "decision_reason": "canonical URL/title deduplication and source/recency ordering",
        "input_items": input_items,
        "output_items": output_items,
        "duplicates_removed": max(0, input_items - output_items),
    }


def _item_identity(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("url", "source_url", "link"):
        canonical = _canonical_url(str(item.get(key) or ""))
        if canonical:
            return "url:" + canonical
    title = str(item.get("title") or item.get("name") or "")
    normalized = re.sub(r"[^\w]+", "", title.casefold())
    return "title:" + normalized if normalized else ""


def _canonical_url(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(sorted(query)), "")
    )


def _item_score(item: Any, *, source: str, serial: int) -> tuple[int, float, int]:
    base_source = source.split(":", 1)[0].replace("fallback_", "")
    source_weight = _SOURCE_WEIGHTS.get(base_source, 10)
    return source_weight, _recency(item), -serial


def _recency(item: Any) -> float:
    if not isinstance(item, dict):
        return 0.0
    for key in ("published_at", "published_date", "updated_at", "date"):
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    return 0.0


def _extract_json(text: str) -> str:
    content = str(text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("reranker returned no JSON object")
    return content[start : end + 1]


def _validate(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    findings = raw.get("ranked_findings")
    if not isinstance(findings, list) or not findings:
        return None
    validated: list[dict[str, Any]] = []
    for item in findings[:10]:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact") or "").strip()
        if not fact:
            continue
        validated.append({
            "fact": fact,
            "source_url": str(item.get("source_url") or ""),
            "source_name": str(item.get("source_name") or ""),
            "confidence": str(item.get("confidence") or "medium"),
        })
    if not validated:
        return None
    try:
        duplicates_merged = int(raw.get("duplicates_merged") or 0)
    except (ValueError, TypeError):
        duplicates_merged = 0
    return {
        "ranked_findings": validated,
        "duplicates_merged": duplicates_merged,
        "overall_confidence": str(raw.get("overall_confidence") or "medium"),
        "gaps": str(raw.get("gaps") or ""),
    }


__all__ = ["ResultReranker", "prepare_results"]
