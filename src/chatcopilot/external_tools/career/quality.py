"""Deterministic evidence source grading and validation."""
from __future__ import annotations

from urllib.parse import urlparse

from chatcopilot.external_tools.career.dates import normalize_date

_GRADE_VALUE = {"A": 1, "B": 2, "C": 3, "D": 4}
_SOURCE_TYPE_CAP = {
    "official": "A",
    "complete_experience": "B",
    "community_post": "C",
    "search_snippet": "D",
    "repost": "D",
}
_COMMUNITY_HOST_SUFFIXES = (
    "xiaohongshu.com",
    "zhihu.com",
    "nowcoder.com",
    "maimai.cn",
)
_SEARCH_HOST_SUFFIXES = (
    "bing.com",
    "google.com",
    "baidu.com",
)


def effective_source_grade(record: dict[str, object]) -> str:
    requested = str(record.get("source_grade") or "D").upper()
    source_type = str(record.get("source_type") or "").strip()
    host = (urlparse(str(record.get("source_url") or "")).hostname or "").casefold()
    cap = _SOURCE_TYPE_CAP.get(source_type, "D")
    if any(host == suffix or host.endswith("." + suffix) for suffix in _COMMUNITY_HOST_SUFFIXES):
        cap = "C"
    if any(host == suffix or host.endswith("." + suffix) for suffix in _SEARCH_HOST_SUFFIXES):
        cap = "D"
    return requested if _GRADE_VALUE[requested] >= _GRADE_VALUE[cap] else cap


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def validate_and_normalize_evidence(record: dict[str, object]) -> dict[str, object]:
    kinds = {"salary", "benefits", "interview_process", "interview_question", "workplace"}
    required = (
        "kind",
        "company",
        "source_name",
        "source_url",
        "source_type",
        "source_grade",
        "published_at",
        "confidence",
    )
    missing = [key for key in required if not str(record.get(key) or "").strip()]
    if missing:
        raise ValueError("证据缺少必填字段: " + ", ".join(missing))
    if record["kind"] not in kinds:
        raise ValueError("kind 不受支持")
    if str(record["source_type"]) not in _SOURCE_TYPE_CAP:
        raise ValueError("source_type 不受支持")
    requested = str(record["source_grade"]).upper()
    if requested not in _GRADE_VALUE:
        raise ValueError("source_grade 必须为 A/B/C/D")
    if record["confidence"] not in {"high", "medium", "low"}:
        raise ValueError("confidence 必须为 high/medium/low")
    if not str(record["source_url"]).startswith(("https://", "http://")):
        raise ValueError("source_url 必须是公开 HTTP(S) 链接")

    normalized = dict(record)
    normalized["source_grade"] = effective_source_grade(normalized)
    normalized["published_on"] = normalize_date(record.get("published_at"))
    if not normalized["published_on"]:
        raise ValueError("published_at 必须是可解析日期")
    if record["kind"] == "interview_question":
        detail_fields = ("role_family", "topic", "normalized_key", "published_at")
        missing_details = [key for key in detail_fields if not str(record.get(key) or "").strip()]
        if missing_details:
            raise ValueError("面试题证据缺少字段: " + ", ".join(missing_details))
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    if record["kind"] == "salary" and payload.get("total_comp"):
        components = ("monthly_salary", "salary_months", "annual_bonus", "stock")
        if not any(payload.get(key) not in (None, "") for key in components):
            raise ValueError("薪资总包必须附带至少一个可核验组成项，不能只保存推测总包")
    return normalized


__all__ = ["effective_source_grade", "validate_and_normalize_evidence"]
