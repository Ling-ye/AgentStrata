"""IFEval adapter.

The official IFEval benchmark ships prompts plus deterministic instruction
checkers. This adapter supports a small built-in smoke subset immediately and
can also ingest official-style JSONL prompt files via CHATCOPILOT_IFEVAL_DATA_PATH.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.official_data import has_ifeval_official_data, ifeval_cache_path
from chatcopilot.evals.selection import balanced_100_cases, normalize_categories, normalize_level

_ENV_DATA_PATH = "CHATCOPILOT_IFEVAL_DATA_PATH"
_ENV_MAX_CASES = "CHATCOPILOT_IFEVAL_MAX_CASES"
_ENV_CASE_PROFILE = "CHATCOPILOT_IFEVAL_CASE_PROFILE"


def load_cases(limit: int | None = None) -> tuple[EvalCase, ...]:
    """Load IFEval cases from official-style JSONL or built-in smoke cases."""

    data_path = os.environ.get(_ENV_DATA_PATH, "").strip()
    if not data_path and has_ifeval_official_data():
        data_path = str(ifeval_cache_path())
    if data_path:
        cases = _load_jsonl_cases(Path(data_path).expanduser())
        if not _manual_case_filter_enabled(limit):
            cases = _select_profile(cases, profile=_case_profile_from_env(), seed=20260614)
    else:
        cases = _smoke_cases()
    max_cases = positive_int_from_env(_ENV_MAX_CASES)
    if max_cases is not None:
        cases = cases[:max_cases]
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return tuple(cases)


def judge(case: EvalCase, final_text: str) -> JudgeResult:
    """Judge a response using the supported deterministic IFEval checks."""

    checks = tuple(case.metadata.get("instruction_checks") or ())
    if not checks:
        return JudgeResult(
            score=0.0,
            max_score=1.0,
            passed=False,
            reasons=("no supported IFEval checks",),
            missing=("instruction_checks",),
        )

    failures: list[str] = []
    for raw_check in checks:
        if not isinstance(raw_check, dict):
            failures.append("invalid_check")
            continue
        check_id = str(raw_check.get("id", "")).strip()
        kwargs = raw_check.get("kwargs") if isinstance(raw_check.get("kwargs"), dict) else {}
        if not _passes_check(check_id, kwargs, final_text):
            failures.append(check_id or "unknown_check")

    total = len(checks)
    passed_checks = total - len(failures)
    score = passed_checks / total if total else 0.0
    return JudgeResult(
        score=score,
        max_score=1.0,
        passed=not failures,
        reasons=("all IFEval checks passed",) if not failures else ("failed IFEval checks",),
        missing=tuple(failures),
    )


def _smoke_cases() -> list[EvalCase]:
    return [
        _case(
            "ifeval-no-comma",
            "请用中文写一句话回答：为什么固定测试集适合观察 Agent 优化？回答中不要使用逗号。",
            [{"id": "punctuation:no_comma", "kwargs": {}}],
        ),
        _case(
            "ifeval-keyword-frequency",
            "请用中文用两句话说明工具调用评测的价值，并且必须至少 2 次提到“工具”。",
            [{"id": "keywords:frequency", "kwargs": {"keyword": "工具", "relation": "at least", "num": 2}}],
        ),
        _case(
            "ifeval-json-format",
            "请只输出 JSON 对象，包含两个字段：name 和 value。不要输出 Markdown。",
            [{"id": "detectable_format:json_object", "kwargs": {}}],
        ),
        _case(
            "ifeval-word-count-max",
            "Answer in English in at most 12 words: what does a baseline compare do?",
            [{"id": "length_constraints:number_words", "kwargs": {"relation": "at most", "num_words": 12}}],
        ),
        _case(
            "ifeval-bullet-count",
            "请列出 3 条评测报告应该包含的信息，每条用 '-' 开头。",
            [{"id": "detectable_format:number_bullets", "kwargs": {"num_bullets": 3}}],
        ),
    ]


def _case(case_id: str, prompt: str, checks: list[dict[str, Any]]) -> EvalCase:
    level = _checks_level(checks, official_instruction_ids=())
    categories = _check_categories(checks, official_instruction_ids=())
    return EvalCase(
        case_id=case_id,
        input=prompt,
        category=categories[0] if categories else "instruction_following",
        expected_behavior="Follow all verifiable instructions in the prompt.",
        metadata={
            "adapter": "ifeval",
            "source": "builtin-smoke",
            "instruction_checks": checks,
            "level": level,
            "problem_categories": categories,
        },
    )


def _load_jsonl_cases(path: Path) -> list[EvalCase]:
    if not path.is_file():
        raise FileNotFoundError(f"IFEval data file not found: {path}")
    cases: list[EvalCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            raw = json.loads(line)
            prompt = str(raw.get("prompt", "")).strip()
            if not prompt:
                raise ValueError(f"{path}:{line_no}: prompt 不能为空")
            key = str(raw.get("key", line_no)).strip()
            checks = _checks_from_official_row(raw)
            if not checks:
                continue
            official_instruction_ids = raw.get("instruction_id_list", [])
            categories = _check_categories(checks, official_instruction_ids=official_instruction_ids)
            cases.append(
                EvalCase(
                    case_id=f"ifeval-{key}",
                    input=prompt,
                    category=categories[0] if categories else "instruction_following",
                    expected_behavior="Follow all verifiable instructions in the prompt.",
                    metadata={
                        "adapter": "ifeval",
                        "source": str(path),
                        "instruction_checks": checks,
                        "official_instruction_ids": official_instruction_ids,
                        "level": _checks_level(checks, official_instruction_ids=official_instruction_ids),
                        "problem_categories": categories,
                    },
                )
            )
    return cases


def _checks_from_official_row(raw: dict[str, Any]) -> list[dict[str, Any]]:
    instruction_ids = raw.get("instruction_id_list") or []
    kwargs_list = raw.get("kwargs") or []
    if not isinstance(instruction_ids, list):
        instruction_ids = []
    if not isinstance(kwargs_list, list):
        kwargs_list = []
    checks: list[dict[str, Any]] = []
    for idx, instruction_id in enumerate(instruction_ids):
        kwargs = kwargs_list[idx] if idx < len(kwargs_list) and isinstance(kwargs_list[idx], dict) else {}
        mapped = _map_official_instruction(str(instruction_id), kwargs)
        if mapped is not None:
            checks.append(mapped)
    return checks


def _map_official_instruction(instruction_id: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    if instruction_id in {
        "punctuation:no_comma",
        "detectable_format:json_format",
        "detectable_format:json_object",
        "length_constraints:number_words",
    }:
        mapped_id = "detectable_format:json_object" if instruction_id == "detectable_format:json_format" else instruction_id
        return {"id": mapped_id, "kwargs": dict(kwargs)}
    if instruction_id in {"keywords:frequency", "keywords:existence"}:
        return {"id": instruction_id, "kwargs": dict(kwargs)}
    return None


def _passes_check(check_id: str, kwargs: dict[str, Any], text: str) -> bool:
    if check_id == "punctuation:no_comma":
        return "," not in text and "，" not in text
    if check_id == "keywords:frequency":
        keyword = str(kwargs.get("keyword", ""))
        relation = str(kwargs.get("relation", "at least")).lower()
        threshold = int(kwargs.get("num", kwargs.get("frequency", 1)) or 1)
        count = text.count(keyword)
        return _compare(count, relation, threshold)
    if check_id == "keywords:existence":
        keywords = kwargs.get("keywords") or kwargs.get("keyword") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        return all(str(keyword) in text for keyword in keywords)
    if check_id == "detectable_format:json_object":
        try:
            parsed = json.loads(_strip_code_fence(text.strip()))
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict)
    if check_id == "length_constraints:number_words":
        relation = str(kwargs.get("relation", "at most")).lower()
        threshold = int(kwargs.get("num_words", kwargs.get("num", 0)) or 0)
        words = re.findall(r"\b[\w'-]+\b", text)
        return _compare(len(words), relation, threshold)
    if check_id == "detectable_format:number_bullets":
        threshold = int(kwargs.get("num_bullets", kwargs.get("num", 0)) or 0)
        bullet_lines = [line for line in text.splitlines() if line.strip().startswith(("-", "*"))]
        return len(bullet_lines) == threshold
    return False


def _compare(value: int, relation: str, threshold: int) -> bool:
    if relation in {"at least", "more than", "greater than", ">="}:
        return value >= threshold
    if relation in {"at most", "less than", "no more than", "<="}:
        return value <= threshold
    if relation in {"exactly", "equal to", "=="}:
        return value == threshold
    return value >= threshold


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


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
            suite_label="IFEval",
        )
    raise ValueError(f"unsupported IFEval case profile: {profile}")


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


def _checks_level(
    checks: list[dict[str, Any]],
    *,
    official_instruction_ids: Any,
) -> str:
    official_count = len(official_instruction_ids) if isinstance(official_instruction_ids, list) else 0
    complexity = max(len(checks), official_count)
    if complexity <= 1:
        return "1"
    if complexity == 2:
        return "2"
    return "3"


def _check_categories(
    checks: list[dict[str, Any]],
    *,
    official_instruction_ids: Any,
) -> tuple[str, ...]:
    ids: list[str] = []
    ids.extend(str(check.get("id", "")) for check in checks if isinstance(check, dict))
    if isinstance(official_instruction_ids, list):
        ids.extend(str(item) for item in official_instruction_ids)
    families = [item.split(":", 1)[0] for item in ids if item]
    return normalize_categories(families or ("instruction_following",))


__all__ = ["judge", "load_cases"]
