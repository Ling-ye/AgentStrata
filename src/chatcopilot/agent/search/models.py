"""Stable request and plan contracts for unified search."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

LOGICAL_SOURCES = ("web", "experience", "commerce", "github", "url")
OPERATIONS = ("search", "read_url", "query_system", "mixed")
READ_STRATEGIES = ("search_only", "search_then_read", "static_then_browser")
VERIFICATION_MODES = ("auto", "required", "none")
DOMAIN_HINTS = ("general", "technical", "game", "consumer", "news")
DEPTH_LEVELS = ("quick", "standard", "thorough")
DEPTH_MAX_STEPS: dict[str, int] = {"quick": 1, "standard": 3, "thorough": 5}
_MAX_OBJECTIVE_CHARS = 4000
_MAX_URLS = 5
_MAX_REQUIRED_FIELDS = 20
_XIAOHONGSHU_RE = re.compile(r"(?<![a-z0-9])xhs(?![a-z0-9])", re.IGNORECASE)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _source_hints(value: Any, *, objective: str) -> tuple[str, ...]:
    hints = [_normalize_source_hint(item) for item in _strings(value)]
    if _mentions_xiaohongshu(objective) and "experience" not in hints:
        hints.insert(0, "experience")
    return tuple(item for item in hints if item)


def _normalize_source_hint(value: str) -> str:
    text = str(value or "").strip()
    folded = text.casefold().replace("-", "_").replace(" ", "_")
    if text == "小红书" or "xiaohongshu" in folded or folded in {"xhs", "xhs_mcp"}:
        return "experience"
    return text


def _mentions_xiaohongshu(text: str) -> bool:
    folded = str(text or "").casefold()
    return "小红书" in folded or "xiaohongshu" in folded or bool(_XIAOHONGSHU_RE.search(folded))


@dataclass(frozen=True)
class SearchRequest:
    objective: str
    urls: tuple[str, ...] = ()
    source_hints: tuple[str, ...] = ()
    domain: str = "general"
    depth: str = "standard"
    time_window: str = "not time-sensitive"
    required_fields: tuple[str, ...] = ("title", "url")
    verification: str = "auto"

    @property
    def max_steps(self) -> int:
        return DEPTH_MAX_STEPS.get(self.depth, 3)

    @classmethod
    def from_args(cls, args: Mapping[str, Any] | None) -> "SearchRequest":
        raw = args or {}
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            raise ValueError("objective cannot be empty")
        if len(objective) > _MAX_OBJECTIVE_CHARS:
            raise ValueError(f"objective cannot exceed {_MAX_OBJECTIVE_CHARS} characters")
        urls = tuple(dict.fromkeys(_strings(raw.get("urls"))))
        if len(urls) > _MAX_URLS:
            raise ValueError(f"at most {_MAX_URLS} URLs may be requested")
        source_hints = tuple(
            dict.fromkeys(_source_hints(raw.get("source_hints"), objective=objective))
        )
        unknown = [item for item in source_hints if item not in LOGICAL_SOURCES]
        if unknown:
            raise ValueError("unknown source_hints: " + ", ".join(unknown))
        if "url" in source_hints and not urls:
            raise ValueError("source_hint 'url' requires at least one concrete URL")
        planned_sources = set(source_hints)
        if urls:
            planned_sources.add("url")
        if len(planned_sources) > 3:
            raise ValueError("at most 3 logical sources may be requested")
        domain = str(raw.get("domain") or "").strip().lower() or "general"
        if domain not in DOMAIN_HINTS:
            domain = "general"
        depth = str(raw.get("depth") or "standard").strip().lower()
        if depth not in DEPTH_LEVELS:
            depth = "standard"
        verification = str(raw.get("verification") or "auto").strip().lower()
        if verification not in VERIFICATION_MODES:
            raise ValueError(
                "verification must be one of: " + ", ".join(VERIFICATION_MODES)
            )
        required_fields = _strings(raw.get("required_fields")) or ("title", "url")
        if len(required_fields) > _MAX_REQUIRED_FIELDS:
            raise ValueError(
                f"at most {_MAX_REQUIRED_FIELDS} required_fields may be requested"
            )
        return cls(
            objective=objective,
            urls=urls,
            source_hints=source_hints,
            domain=domain,
            depth=depth,
            time_window=str(raw.get("time_window") or "not time-sensitive").strip()
            or "not time-sensitive",
            required_fields=required_fields,
            verification=verification,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "urls": list(self.urls),
            "source_hints": list(self.source_hints),
            "domain": self.domain,
            "depth": self.depth,
            "time_window": self.time_window,
            "required_fields": list(self.required_fields),
            "verification": self.verification,
        }


@dataclass(frozen=True)
class SearchAction:
    source: str
    query: str = ""
    urls: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    read_strategy: str = "search_then_read"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query": self.query,
            "urls": list(self.urls),
            "required_fields": list(self.required_fields),
            "read_strategy": self.read_strategy,
        }


@dataclass(frozen=True)
class SearchPlan:
    operation: str
    steps: tuple[SearchAction, ...]
    cross_check: bool = False
    route_source: str = "llm"
    route_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "steps": [step.to_dict() for step in self.steps],
            "cross_check": self.cross_check,
            "route_source": self.route_source,
            "route_reason": self.route_reason,
            "decision_source": self.route_source,
            "decision_reason": self.route_reason,
        }


ResearchRequest = SearchRequest
ResearchStep = SearchAction
ResearchPlan = SearchPlan

__all__ = [
    "DEPTH_LEVELS",
    "DEPTH_MAX_STEPS",
    "DOMAIN_HINTS",
    "LOGICAL_SOURCES",
    "OPERATIONS",
    "READ_STRATEGIES",
    "ResearchPlan",
    "ResearchRequest",
    "ResearchStep",
    "SearchAction",
    "SearchPlan",
    "SearchRequest",
    "VERIFICATION_MODES",
]
