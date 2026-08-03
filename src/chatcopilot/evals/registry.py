"""Evaluation suite registry."""

from __future__ import annotations

from chatcopilot.evals.catalog import STANDARDS
from chatcopilot.evals.adapters import bfcl, gaia, ifeval, swebench, webarena
from chatcopilot.evals.models import BenchmarkStandard, EvalCase

_STANDARDS_BY_ID = {standard.suite_id: standard for standard in STANDARDS}


def list_standards() -> tuple[BenchmarkStandard, ...]:
    """Return all manually selectable benchmark standards."""

    return STANDARDS


def get_standard(suite_id: str) -> BenchmarkStandard:
    """Resolve a suite id into metadata."""

    normalized = normalize_suite_id(suite_id)
    try:
        return _STANDARDS_BY_ID[normalized]
    except KeyError as exc:
        known = ", ".join(sorted(_STANDARDS_BY_ID))
        raise ValueError(f"未知评测标准: {suite_id}；可选: {known}") from exc


def get_cases(
    suite_id: str,
    *,
    auto_prepare: bool = True,
) -> tuple[EvalCase, ...]:
    """Return built-in cases for a suite. Public benchmarks may require external data."""

    normalized = normalize_suite_id(suite_id)
    if normalized == "gaia":
        return gaia.load_cases(auto_download=auto_prepare)
    if normalized == "ifeval":
        return ifeval.load_cases()
    if normalized == "bfcl":
        return bfcl.load_cases()
    if normalized == "swe-bench-verified":
        return swebench.load_cases()
    if normalized == "webarena":
        return webarena.load_cases()
    return ()


def normalize_suite_id(value: str) -> str:
    return value.strip().lower().replace("_", "-")
