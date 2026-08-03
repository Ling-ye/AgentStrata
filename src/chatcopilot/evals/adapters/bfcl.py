"""BFCL (Berkeley Function Calling Leaderboard) adapter.

Evaluates LLM function/tool calling accuracy using the official BFCL dataset.
Supports a built-in smoke subset for immediate use and external JSONL data via
CHATCOPILOT_BFCL_DATA_DIR.

Scoring uses deterministic AST matching: compare function names and argument
values between model output and ground truth, ignoring argument order.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.official_data import bfcl_cache_dir, has_bfcl_official_data
from chatcopilot.evals.selection import balanced_100_cases, normalize_categories, normalize_level

_ENV_DATA_DIR = "CHATCOPILOT_BFCL_DATA_DIR"
_ENV_MAX_CASES = "CHATCOPILOT_BFCL_MAX_CASES"
_ENV_CATEGORY = "CHATCOPILOT_BFCL_CATEGORY"
_ENV_CASE_PROFILE = "CHATCOPILOT_BFCL_CASE_PROFILE"

_SUPPORTED_CATEGORIES = ("simple", "multiple", "parallel", "parallel_multiple", "relevance")

_DATA_FILES = {
    "simple": "BFCL_v3_simple.json",
    "multiple": "BFCL_v3_multiple.json",
    "parallel": "BFCL_v3_parallel.json",
    "parallel_multiple": "BFCL_v3_parallel_multiple.json",
    "relevance": "BFCL_v3_irrelevance.json",
}
_ANSWER_FILES = {
    "simple": "possible_answer/BFCL_v3_simple.json",
    "multiple": "possible_answer/BFCL_v3_multiple.json",
    "parallel": "possible_answer/BFCL_v3_parallel.json",
    "parallel_multiple": "possible_answer/BFCL_v3_parallel_multiple.json",
    "relevance": "possible_answer/BFCL_v3_irrelevance.json",
}


def load_cases(
    limit: int | None = None,
    *,
    category: str | None = None,
) -> tuple[EvalCase, ...]:
    """Load BFCL cases from external data or built-in smoke subset."""

    data_dir = os.environ.get(_ENV_DATA_DIR, "").strip()
    if not data_dir and has_bfcl_official_data():
        data_dir = str(bfcl_cache_dir())
    cat_filter = category or os.environ.get(_ENV_CATEGORY, "").strip() or None

    if data_dir:
        cases = _load_external_cases(Path(data_dir).expanduser(), category=cat_filter)
        if not cat_filter and not _manual_case_filter_enabled(limit):
            cases = _select_profile(cases, profile=_case_profile_from_env(), seed=20260614)
    else:
        cases = _smoke_cases()
        if cat_filter:
            cases = [c for c in cases if c.metadata.get("bfcl_category") == cat_filter]

    max_cases = positive_int_from_env(_ENV_MAX_CASES)
    if max_cases is not None:
        cases = cases[:max_cases]
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return tuple(cases)


def judge(case: EvalCase, tool_calls: list[dict[str, Any]]) -> JudgeResult:
    """Score model tool_calls against ground truth using AST matching."""

    expected = case.metadata.get("expected_calls")
    if expected is None or not isinstance(expected, list):
        return JudgeResult(
            score=0.0,
            max_score=1.0,
            passed=False,
            reasons=("no expected_calls in case metadata",),
            missing=("expected_calls",),
        )

    category = str(case.metadata.get("bfcl_category", "simple"))

    if category == "relevance":
        return _judge_relevance(expected, tool_calls)

    if category in ("parallel", "parallel_multiple"):
        return _judge_parallel(expected, tool_calls)

    return _judge_sequential(expected, tool_calls)


def build_tools_schema(case: EvalCase) -> list[dict[str, Any]]:
    """Build OpenAI-compatible tools array from case metadata."""

    functions = case.metadata.get("functions") or []
    return [
        {
            "type": "function",
            "function": fn,
        }
        for fn in functions
        if isinstance(fn, dict)
    ]


def build_messages(case: EvalCase) -> list[dict[str, Any]]:
    """Build chat messages from case input."""

    return [{"role": "user", "content": case.input}]


# ---------------------------------------------------------------------------
# Judging strategies
# ---------------------------------------------------------------------------

def _judge_sequential(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> JudgeResult:
    """Single or multiple function calls, order matters."""

    if not expected:
        passed = len(actual) == 0
        return JudgeResult(
            score=1.0 if passed else 0.0,
            max_score=1.0,
            passed=passed,
            reasons=("no calls expected",) if passed else ("unexpected tool calls",),
        )

    total = len(expected)
    matched = 0
    missing: list[str] = []

    for idx, exp_call in enumerate(expected):
        if idx < len(actual) and _calls_match(exp_call, actual[idx]):
            matched += 1
        else:
            missing.append(_call_label(exp_call, idx))

    score = matched / total if total else 0.0
    return JudgeResult(
        score=score,
        max_score=1.0,
        passed=matched == total,
        reasons=("all calls matched",) if matched == total else ("mismatched calls",),
        missing=tuple(missing),
    )


def _judge_parallel(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> JudgeResult:
    """Parallel calls: order doesn't matter, match by best fit."""

    if not expected:
        passed = len(actual) == 0
        return JudgeResult(
            score=1.0 if passed else 0.0, max_score=1.0, passed=passed,
            reasons=("no calls expected",) if passed else ("unexpected tool calls",),
        )

    total = len(expected)
    remaining = list(actual)
    matched = 0
    missing: list[str] = []

    for idx, exp_call in enumerate(expected):
        found = -1
        for j, act_call in enumerate(remaining):
            if _calls_match(exp_call, act_call):
                found = j
                break
        if found >= 0:
            remaining.pop(found)
            matched += 1
        else:
            missing.append(_call_label(exp_call, idx))

    score = matched / total if total else 0.0
    return JudgeResult(
        score=score,
        max_score=1.0,
        passed=matched == total,
        reasons=("all parallel calls matched",) if matched == total else ("mismatched parallel calls",),
        missing=tuple(missing),
    )


def _judge_relevance(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> JudgeResult:
    """Irrelevance detection: model should NOT produce any tool calls."""

    if not actual:
        return JudgeResult(score=1.0, max_score=1.0, passed=True, reasons=("correctly refused to call",))
    return JudgeResult(
        score=0.0,
        max_score=1.0,
        passed=False,
        reasons=("should not have called any function",),
        violations=tuple(_call_label(c, i) for i, c in enumerate(actual)),
    )


# ---------------------------------------------------------------------------
# Call matching
# ---------------------------------------------------------------------------

def _calls_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    exp_name = _norm_name(expected)
    act_name = _norm_name(actual)
    if exp_name != act_name:
        return False
    exp_args = _norm_args(expected)
    act_args = _norm_args(actual)
    return _args_match(exp_args, act_args)


def _norm_name(call: dict[str, Any]) -> str:
    name = call.get("name") or ""
    fn = call.get("function") or {}
    if isinstance(fn, dict):
        name = fn.get("name") or name
    return str(name).strip()


def _norm_args(call: dict[str, Any]) -> dict[str, Any]:
    args = call.get("arguments") or call.get("args") or {}
    fn = call.get("function") or {}
    if isinstance(fn, dict):
        args = fn.get("arguments") or args
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            pass
    return args if isinstance(args, dict) else {}


def _args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, exp_val in expected.items():
        act_val = actual.get(key)
        if not _values_match(exp_val, act_val):
            return False
    return True


def _values_match(expected: Any, actual: Any) -> bool:
    if expected is None:
        return True
    if isinstance(expected, dict) and isinstance(actual, dict):
        return _args_match(expected, actual)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(_values_match(e, a) for e, a in zip(expected, actual))
    exp_str = str(expected).strip().lower()
    act_str = str(actual).strip().lower() if actual is not None else ""
    return exp_str == act_str


def _call_label(call: dict[str, Any], index: int) -> str:
    name = _norm_name(call) or f"call[{index}]"
    return f"{name}@{index}"


# ---------------------------------------------------------------------------
# External data loading
# ---------------------------------------------------------------------------

def _load_external_cases(
    data_dir: Path,
    *,
    category: str | None = None,
) -> list[EvalCase]:
    if not data_dir.is_dir():
        raise FileNotFoundError(f"BFCL data directory not found: {data_dir}")

    categories = [category] if category and category in _SUPPORTED_CATEGORIES else list(_DATA_FILES)
    cases: list[EvalCase] = []

    for cat in categories:
        data_file = data_dir / _DATA_FILES[cat]
        answer_file = data_dir / _ANSWER_FILES[cat]
        if not data_file.is_file():
            continue
        answers = _load_answers(answer_file) if answer_file.is_file() else {}
        for row in _read_jsonl(data_file):
            case = _row_to_case(row, cat, answers)
            if case is not None:
                cases.append(case)

    return cases


def _load_answers(path: Path) -> dict[str, Any]:
    """Load ground truth answers keyed by id."""

    mapping: dict[str, Any] = {}
    for row in _read_jsonl(path):
        case_id = str(row.get("id", "")).strip()
        if case_id:
            mapping[case_id] = row.get("ground_truth") or row.get("result") or row.get("expected")
    return mapping


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _row_to_case(
    row: dict[str, Any],
    category: str,
    answers: dict[str, Any],
) -> EvalCase | None:
    case_id = str(row.get("id", "")).strip()
    if not case_id:
        return None

    question = _extract_question(row)
    if not question:
        return None

    functions = row.get("function") or []
    if not isinstance(functions, list):
        functions = [functions]

    expected_calls = _parse_expected_calls(answers.get(case_id), row)
    level = _category_level(category)

    return EvalCase(
        case_id=f"bfcl-{case_id}",
        input=question,
        category=f"bfcl-{category}",
        expected_behavior="Generate correct function calls matching the ground truth.",
        metadata={
            "adapter": "bfcl",
            "bfcl_category": category,
            "level": level,
            "problem_categories": _bfcl_case_categories(category, functions, expected_calls),
            "functions": functions,
            "expected_calls": expected_calls,
            "source": "external",
        },
    )


def _extract_question(row: dict[str, Any]) -> str:
    raw = row.get("question")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        for turn in raw:
            if isinstance(turn, list):
                for msg in turn:
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        return str(msg.get("content", "")).strip()
            elif isinstance(turn, dict) and turn.get("role") == "user":
                return str(turn.get("content", "")).strip()
    return ""


def _parse_expected_calls(
    ground_truth: Any,
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse ground truth into normalized call dicts."""

    if ground_truth is not None:
        return _normalize_ground_truth(ground_truth)

    raw = row.get("ground_truth") or row.get("result") or row.get("expected")
    if raw is not None:
        return _normalize_ground_truth(raw)
    return []


def _normalize_ground_truth(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return _parse_function_call_string(raw)
        return _normalize_ground_truth(parsed)

    if isinstance(raw, dict):
        if "name" in raw or "function" in raw:
            return [raw]
        calls: list[dict[str, Any]] = []
        for fn_name, args in raw.items():
            calls.append({"name": fn_name, "arguments": args if isinstance(args, dict) else {}})
        return calls

    if isinstance(raw, list):
        result: list[dict[str, Any]] = []
        for item in raw:
            result.extend(_normalize_ground_truth(item))
        return result

    return []


def _parse_function_call_string(text: str) -> list[dict[str, Any]]:
    """Parse 'func_name(arg1=val1, arg2=val2)' style strings."""

    calls: list[dict[str, Any]] = []
    for match in re.finditer(r"(\w+)\(([^)]*)\)", text):
        fn_name = match.group(1)
        args_str = match.group(2).strip()
        args: dict[str, Any] = {}
        if args_str:
            try:
                args = dict(ast.literal_eval(f"dict({args_str})"))
            except (ValueError, SyntaxError):
                for pair in args_str.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        args[k.strip()] = _try_parse_value(v.strip())
        calls.append({"name": fn_name, "arguments": args})
    return calls


def _try_parse_value(raw: str) -> Any:
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(raw)
        except (ValueError, SyntaxError, json.JSONDecodeError):
            continue
    return raw.strip("'\"")


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
    return _normalize_profile(os.environ.get(_ENV_CASE_PROFILE, "balanced-100"))


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
                  "radius": {"type": "number", "description": "Radius of the circle"}},
                  "required": ["radius"]}}],
            [{"name": "circle_area", "arguments": {"radius": 5}}],
        ),
        _smoke(
            "bfcl-smoke-multiple",
            "multiple",
            "Search for 'python tutorial' and also bookmark the page 'https://docs.python.org'.",
            [{"name": "web_search", "description": "Search the web",
              "parameters": {"type": "object", "properties": {
                  "query": {"type": "string"}}, "required": ["query"]}},
             {"name": "bookmark_page", "description": "Bookmark a URL",
              "parameters": {"type": "object", "properties": {
                  "url": {"type": "string"}}, "required": ["url"]}}],
            [{"name": "web_search", "arguments": {"query": "python tutorial"}},
             {"name": "bookmark_page", "arguments": {"url": "https://docs.python.org"}}],
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
            "relevance",
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
    return EvalCase(
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
        },
    )


__all__ = ["build_messages", "build_tools_schema", "judge", "load_cases"]
