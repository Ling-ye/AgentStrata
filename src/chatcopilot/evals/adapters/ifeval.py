"""IFEval adapter.

The official IFEval benchmark ships prompts plus deterministic instruction
checkers. This adapter supports a small built-in smoke subset immediately and
can also ingest official-style JSONL prompt files via CHATCOPILOT_IFEVAL_DATA_PATH.
"""

from __future__ import annotations

import json
import os
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
    results = instruction_results(case, final_text)
    passed = all(item["passed"] for item in results)
    return JudgeResult(float(passed), 1.0, passed,
        reasons=tuple(item["id"] + (": passed" if item["passed"] else ": failed") for item in results))


def instruction_results(case: EvalCase, output: str) -> list[dict[str, Any]]:
    from chatcopilot.evals.ifeval_official import check_instructions
    checks = case.metadata["instruction_checks"]
    return check_instructions([c["id"] for c in checks], [c["kwargs"] for c in checks], output, case.input)


def preflight(cases) -> None:
    from chatcopilot.evals.ifeval_official import build_checker
    for case in cases:
        for check in case.metadata["instruction_checks"]:
            build_checker(check["id"], check["kwargs"], case.input)
            if check["id"] == "length_constraints:number_sentences":
                from chatcopilot.evals.vendor.ifeval.instructions_util import _get_sentence_tokenizer
                _get_sentence_tokenizer()


def _smoke_cases() -> list[EvalCase]:
    from importlib.resources import files
    rows = json.loads(files("chatcopilot.evals.suites").joinpath("ifeval/fixtures/fixed.json").read_text())["rows"]
    return [_row_case(row, "fixed-official-subset", prefix="ifeval-fixed") for row in rows]


def _case(case_id: str, prompt: str, checks: list[dict[str, Any]]) -> EvalCase:
    return EvalCase(case_id=case_id, input=prompt, category="instruction_following",
        capability_tags=("指令遵循",), expected_behavior="Follow every declared verifiable instruction.",
        metadata={"adapter": "ifeval", "source": "fixed-official-subset", "instruction_checks": checks,
                  "level": str(min(3, len(checks))), "problem_categories": ["instruction_following"]})


def _row_case(raw: dict[str, Any], source: str, prefix: str = "ifeval") -> EvalCase:
    from chatcopilot.evals.ifeval_official import REVISION
    ids, args = raw.get("instruction_id_list"), raw.get("kwargs")
    if not isinstance(ids, list) or not ids or not isinstance(args, list) or len(ids) != len(args):
        raise ValueError("IFEval instruction/parameter count mismatch")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("IFEval prompt is missing")
    checks = [{"id": ident, "kwargs": value} for ident, value in zip(ids, args)]
    case = _case(f"{prefix}-{raw['key']}", prompt, checks)
    case.metadata.update(source=source, source_revision=REVISION, official_instruction_ids=ids)
    preflight([case])
    return case


def _load_jsonl_cases(path: Path) -> list[EvalCase]:
    cases = [_row_case(json.loads(line), str(path)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({c.case_id for c in cases}) != len(cases):
        raise ValueError("duplicate IFEval Case ID")
    return cases


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
