"""Load frozen SWE-bench instances; execution is owned by the environment plugin."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.evals.models import EvalCase

_ENV_DATA_PATH = "CHATCOPILOT_SWEBENCH_DATA_PATH"
_ENV_MAX_CASES = "CHATCOPILOT_SWEBENCH_MAX_CASES"

_ID_KEYS = ("instance_id", "id")
_REPO_KEYS = ("repo",)
_PROBLEM_KEYS = ("problem_statement",)



def load_cases(limit: int | None = None) -> tuple[EvalCase, ...]:
    """Load SWE-bench Verified cases from official JSONL."""

    data_path = os.environ.get(_ENV_DATA_PATH, "").strip()
    if not data_path:
        from chatcopilot.evals.benchmark_data import swe_data_path

        cached = swe_data_path()
        if cached is None:
            return ()
        data_path = str(cached)

    path = Path(data_path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise FileNotFoundError(f"SWE-bench data not found: {path}")

    cases: list[EvalCase] = []
    from chatcopilot.evals.benchmark_data import swe_data_path, SWE_REVISION

    official = swe_data_path()
    revision = SWE_REVISION if official and path.resolve() == official.resolve() else ""
    for row in _read_jsonl(path):
        case = _row_to_case(row)
        if case is not None:
            cases.append(replace(case, metadata={**case.metadata, "source_revision": revision,
                                                  "source": str(path)}))

    max_cases = positive_int_from_env(_ENV_MAX_CASES)
    if max_cases is not None:
        cases = cases[:max_cases]
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return tuple(cases)


def _row_to_case(row: dict[str, Any]) -> EvalCase | None:
    instance_id = _first(row, _ID_KEYS)
    problem = _first(row, _PROBLEM_KEYS)
    if not instance_id or not problem:
        return None
    repo = _first(row, _REPO_KEYS)
    return EvalCase(
        capability_tags=("代码修复",),
        case_id=f"swebench-{instance_id}",
        input=problem,
        category=f"swebench-{repo}" if repo else "swebench",
        expected_behavior="Generate a patch that makes the repository's test suite pass.",
        metadata={
            "adapter": "swebench",
            "instance_id": instance_id,
            "repo": repo,
            "base_commit": row.get("base_commit", ""),
            "swe_instance": {key: value for key, value in row.items() if key not in {"patch", "gold_patch", "hints_text"}},
        },
    )


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = row.get(key)
        if val is not None:
            text = str(val).strip()
            if text:
                return text
    return ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    return rows


__all__ = ["load_cases"]
