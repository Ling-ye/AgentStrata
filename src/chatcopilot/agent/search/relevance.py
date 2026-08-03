"""Lightweight relevance filtering for noisy search engines."""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlparse

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\-\u4e00-\u9fff]{2,}")
_MAX_ITEMS = 15


def filter_relevant_items(
    items: Iterable[dict[str, Any]],
    *,
    query: str,
    min_score: int = 1,
    max_items: int = _MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Return best search items after cheap token/host scoring."""

    query_tokens = _tokens(query)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        score = _score_item(item, query_tokens)
        if score >= min_score:
            enriched = dict(item)
            enriched["relevance_score"] = score
            scored.append((score, -index, enriched))
    scored.sort(reverse=True)
    return [item for _score, _order, item in scored[:max_items]]


def _score_item(item: dict[str, Any], query_tokens: set[str]) -> int:
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or "")
    url = str(item.get("url") or "")
    host = urlparse(url).netloc.casefold()
    haystack = f"{title} {snippet} {host}".casefold()
    score = 0
    for token in query_tokens:
        if token in haystack:
            score += 1
        if token in title.casefold():
            score += 1
        if token in host:
            score += 1
    if url.startswith("http"):
        score += 1
    if _looks_like_noise(title, snippet, host):
        score -= 3
    return score


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(str(text or ""))
        if token.casefold() not in {"the", "and", "for", "with", "latest", "最新"}
    }


def _looks_like_noise(title: str, snippet: str, host: str) -> bool:
    text = f"{title} {snippet} {host}".casefold()
    noise_markers = (
        "captcha",
        "robot check",
        "unusual traffic",
        "login",
        "sign in",
        "广告",
        "推广",
    )
    return any(marker in text for marker in noise_markers)


__all__ = ["filter_relevant_items"]
