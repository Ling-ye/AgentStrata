"""SWE-bench Verified adapter (interface stub).

Full implementation requires Docker sandbox infrastructure:
clone repo → worktree → Agent patch → Docker test execution.

Currently only supports data loading; ``prepare_task`` and ``judge``
raise ``NotImplementedError`` until the sandbox is built.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.evals.models import EvalCase, JudgeResult

_ENV_DATA_PATH = "CHATCOPILOT_SWEBENCH_DATA_PATH"
_ENV_MAX_CASES = "CHATCOPILOT_SWEBENCH_MAX_CASES"

_ID_KEYS = ("instance_id", "id")
_REPO_KEYS = ("repo",)
_PROBLEM_KEYS = ("problem_statement",)
_PATCH_KEYS = ("patch", "gold_patch")


def load_cases(limit: int | None = None) -> tuple[EvalCase, ...]:
    """Load SWE-bench Verified cases from official JSONL."""

    data_path = os.environ.get(_ENV_DATA_PATH, "").strip()
    if not data_path:
        return ()

    path = Path(data_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"SWE-bench data not found: {path}")

    cases: list[EvalCase] = []
    for row in _read_jsonl(path):
        case = _row_to_case(row)
        if case is not None:
            cases.append(case)

    max_cases = positive_int_from_env(_ENV_MAX_CASES)
    if max_cases is not None:
        cases = cases[:max_cases]
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return tuple(cases)


def prepare_task(case: EvalCase, workspace: Any) -> Any:
    raise NotImplementedError(
        "SWE-bench 需要 Docker 沙箱基础设施（clone repo → worktree → Agent 修改 → Docker 测试）。"
    )


def judge(case: EvalCase, patch: str) -> JudgeResult:
    raise NotImplementedError(
        "SWE-bench 判分需要 Docker 沙箱（apply patch → 运行测试套件 → 检查通过率）。"
    )


def _row_to_case(row: dict[str, Any]) -> EvalCase | None:
    instance_id = _first(row, _ID_KEYS)
    problem = _first(row, _PROBLEM_KEYS)
    if not instance_id or not problem:
        return None
    repo = _first(row, _REPO_KEYS)
    return EvalCase(
        case_id=f"swebench-{instance_id}",
        input=problem,
        category=f"swebench-{repo}" if repo else "swebench",
        expected_behavior="Generate a patch that makes the repository's test suite pass.",
        metadata={
            "adapter": "swebench",
            "instance_id": instance_id,
            "repo": repo,
            "base_commit": row.get("base_commit", ""),
            "test_patch": row.get("test_patch", ""),
            "gold_patch": _first(row, _PATCH_KEYS),
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


__all__ = ["judge", "load_cases", "prepare_task"]
