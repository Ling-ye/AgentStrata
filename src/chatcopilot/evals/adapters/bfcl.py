"""BFCL V4 official single-turn adapter; smoke is an explicit debug profile."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.official_data import bfcl_cache_dir, has_bfcl_official_data
from chatcopilot.evals.selection import balanced_100_cases, normalize_categories, normalize_level
from chatcopilot.evals import bfcl_official as official

_ENV_DATA_DIR = "CHATCOPILOT_BFCL_DATA_DIR"
_ENV_MAX_CASES = "CHATCOPILOT_BFCL_MAX_CASES"
_ENV_CATEGORY = "CHATCOPILOT_BFCL_CATEGORY"
_ENV_CASE_PROFILE = "CHATCOPILOT_BFCL_CASE_PROFILE"


def load_cases(limit: int | None = None, *, category: str | None = None) -> tuple[EvalCase, ...]:
    profile = _case_profile_from_env()
    cat = category or os.environ.get(_ENV_CATEGORY, "").strip()
    if profile == "smoke":
        cases = _smoke_cases()
    else:
        raw = os.environ.get(_ENV_DATA_DIR, "").strip()
        if not raw and not has_bfcl_official_data():
            return ()
        cases = list(official.rows_to_cases(Path(raw).expanduser() if raw else bfcl_cache_dir()))
        if not cat and not _manual_case_filter_enabled(limit):
            cases = _select_profile(cases, profile=profile, seed=20260614)
    if cat:
        choices = {c.metadata["bfcl_category"] for c in cases}
        if cat not in choices:
            raise ValueError(f"unsupported BFCL category: {cat}")
        cases = [c for c in cases if c.metadata["bfcl_category"] == cat]
    maximum = positive_int_from_env(_ENV_MAX_CASES)
    if maximum is not None:
        cases = cases[:maximum]
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return tuple(cases)


def judge(case: EvalCase, tool_calls: list[dict[str, Any]]) -> JudgeResult:
    return official.judge(case, tool_calls)


def build_tools_schema(case: EvalCase) -> list[dict[str, Any]]:
    return official.tools_for(case.metadata["functions"], category=case.metadata["bfcl_category"])


def build_messages(case: EvalCase) -> list[dict[str, Any]]:
    return case.metadata.get("question_messages") or [{"role": "user", "content": case.input}]


def _select_profile(cases: list[EvalCase], *, profile: str, seed: int) -> list[EvalCase]:
    normalized = _normalize_profile(profile)
    if normalized in {"", "full"}:
        return cases
    if normalized == "balanced-100":
        return balanced_100_cases(
            cases,
            level_of=lambda case: normalize_level(case.metadata.get("level")),
            categories_of=_case_categories,
            seed=seed,
            suite_label="BFCL",
        )
    raise ValueError(f"unsupported BFCL case profile: {profile}")


def _manual_case_filter_enabled(limit: int | None) -> bool:
    return any(
        (
            limit is not None and limit > 0,
            bool(os.environ.get(_ENV_MAX_CASES, "").strip()),
        )
    )


def _case_profile_from_env() -> str:
    return _normalize_profile(os.environ.get(_ENV_CASE_PROFILE, "full"))


def _normalize_profile(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _case_categories(case: EvalCase) -> tuple[str, ...]:
    return normalize_categories(case.metadata.get("problem_categories") or (case.category,))


def _category_level(category: str) -> str:
    return {
        "simple": "1",
        "relevance": "1",
        "multiple": "2",
        "parallel": "3",
        "parallel_multiple": "3",
    }.get(category, "")


def _bfcl_case_categories(
    category: str,
    functions: list[dict[str, Any]],
    expected_calls: list[dict[str, Any]],
) -> tuple[str, ...]:
    tags = [category, f"functions:{len(functions)}", f"calls:{len(expected_calls)}"]
    if not expected_calls:
        tags.append("no_call")
    if len(expected_calls) > 1:
        tags.append("multi_call")
    return normalize_categories(tags)


# ---------------------------------------------------------------------------
# Built-in smoke cases
# ---------------------------------------------------------------------------

def _smoke_cases() -> list[EvalCase]:
    """Minimal hand-crafted cases to validate the BFCL pipeline without external data."""

    return [
        _smoke(
            "bfcl-smoke-simple-weather",
            "simple",
            "What's the weather in San Francisco?",
            [{"name": "get_weather", "description": "Get current weather for a city",
              "parameters": {"type": "object", "properties": {
                  "city": {"type": "string", "description": "City name"},
                  "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
                  "required": ["city"]}}],
            [{"name": "get_weather", "arguments": {"city": "San Francisco"}}],
        ),
        _smoke(
            "bfcl-smoke-simple-math",
            "simple",
            "Calculate the area of a circle with radius 5.",
            [{"name": "circle_area", "description": "Calculate circle area",
              "parameters": {"type": "object", "properties": {
                  "radius": {"type": "float", "description": "Radius of the circle"}},
                  "required": ["radius"]}}],
            [{"name": "circle_area", "arguments": {"radius": 5}}],
        ),
        _smoke(
            "bfcl-smoke-multiple",
            "multiple",
            "Search for 'python tutorial'.",
            [{"name": "web_search", "description": "Search the web",
              "parameters": {"type": "object", "properties": {
                  "query": {"type": "string"}}, "required": ["query"]}},
             {"name": "bookmark_page", "description": "Bookmark a URL",
              "parameters": {"type": "object", "properties": {
                  "url": {"type": "string"}}, "required": ["url"]}}],
            [{"name": "web_search", "arguments": {"query": "python tutorial"}}],
        ),
        _smoke(
            "bfcl-smoke-parallel",
            "parallel",
            "Get the weather in both Tokyo and London in celsius.",
            [{"name": "get_weather", "description": "Get current weather for a city",
              "parameters": {"type": "object", "properties": {
                  "city": {"type": "string"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
                  "required": ["city"]}}],
            [{"name": "get_weather", "arguments": {"city": "Tokyo", "unit": "celsius"}},
             {"name": "get_weather", "arguments": {"city": "London", "unit": "celsius"}}],
        ),
        _smoke(
            "bfcl-smoke-relevance",
            "irrelevance",
            "Tell me a joke about programming.",
            [{"name": "get_stock_price", "description": "Get stock price by ticker",
              "parameters": {"type": "object", "properties": {
                  "ticker": {"type": "string"}}, "required": ["ticker"]}}],
            [],
        ),
    ]


def _smoke(
    case_id: str,
    category: str,
    question: str,
    functions: list[dict[str, Any]],
    expected_calls: list[dict[str, Any]],
) -> EvalCase:
    candidates = []
    for call in expected_calls:
        args = {key: [value] for key, value in call["arguments"].items()}
        fn = next(f for f in functions if f["name"] == call["name"])
        for key, schema in fn["parameters"]["properties"].items():
            if key not in args and key not in fn["parameters"].get("required", []):
                args[key] = ["", *schema.get("enum", [])]
        candidates.append({call["name"]: args})
    return EvalCase(
        capability_tags=("函数调用",),
        case_id=case_id,
        input=question,
        category=f"bfcl-{category}",
        expected_behavior="Generate correct function calls matching the ground truth.",
        metadata={
            "adapter": "bfcl",
            "bfcl_category": category,
            "level": _category_level(category),
            "problem_categories": _bfcl_case_categories(category, functions, expected_calls),
            "functions": functions,
            "expected_calls": expected_calls,
            "source": "builtin-smoke",
            "language": "Python",
            "possible_answer": candidates,
        },
    )


__all__ = ["build_messages", "build_tools_schema", "judge", "load_cases"]
