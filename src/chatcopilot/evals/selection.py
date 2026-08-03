"""Deterministic case selection helpers for evaluation suites."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable
from typing import TypeVar

from chatcopilot.evals.models import EvalCase

T = TypeVar("T", bound=EvalCase)

BALANCED_100_LEVEL_TARGETS: dict[str, int] = {"1": 34, "2": 33, "3": 33}


def balanced_100_cases(
    cases: Iterable[T],
    *,
    level_of: Callable[[T], str],
    categories_of: Callable[[T], Iterable[str]],
    seed: int = 20260614,
    suite_label: str = "suite",
) -> list[T]:
    """Select 100 cases by preferred level quota while maximizing category coverage."""

    case_list = list(cases)
    if len(case_list) < 100:
        raise ValueError(f"{suite_label} balanced-100 requires 100 cases, got {len(case_list)}")
    selected: list[T] = []
    selected_ids: set[str] = set()
    for level, target in BALANCED_100_LEVEL_TARGETS.items():
        bucket = [case for case in case_list if level_of(case) == level]
        level_selected = _select_with_category_coverage(
            bucket,
            target=min(target, len(bucket)),
            categories_of=categories_of,
            seed=seed + int(level),
        )
        selected.extend(level_selected)
        selected_ids.update(case.case_id for case in level_selected)

    if len(selected) < 100:
        fillers = [case for case in case_list if case.case_id not in selected_ids]
        selected.extend(
            _select_with_category_coverage(
                fillers,
                target=100 - len(selected),
                categories_of=categories_of,
                seed=seed + 100,
            )
        )
    return selected[:100]


def normalize_level(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("level"):
        text = text.removeprefix("level").strip(" -_:")
    if text in {"1", "2", "3"}:
        return text
    return ""


def normalize_categories(values: Iterable[object]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        text = " ".join(text.replace("_", " ").replace("-", " ").split())
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _select_with_category_coverage(
    cases: list[T],
    *,
    target: int,
    categories_of: Callable[[T], Iterable[str]],
    seed: int,
) -> list[T]:
    rng = random.Random(seed)
    ordered = sorted(cases, key=lambda case: case.case_id)
    rng.shuffle(ordered)

    selected: list[T] = []
    selected_ids: set[str] = set()
    categories = sorted({category for case in ordered for category in categories_of(case)})

    for category in categories:
        if len(selected) >= target:
            break
        candidate = next(
            (
                case
                for case in ordered
                if case.case_id not in selected_ids and category in set(categories_of(case))
            ),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
            selected_ids.add(candidate.case_id)

    for case in ordered:
        if len(selected) >= target:
            break
        if case.case_id in selected_ids:
            continue
        selected.append(case)
        selected_ids.add(case.case_id)

    return selected


__all__ = [
    "BALANCED_100_LEVEL_TARGETS",
    "balanced_100_cases",
    "normalize_categories",
    "normalize_level",
]
