"""WebArena adapter (interface stub).

Full implementation requires:
- Agent browser automation tools (click, type, navigate, screenshot)
- Self-hosted WebArena Web application cluster (Reddit/GitLab/Shopping Docker)

Currently only placeholder; ``load_cases`` returns empty tuple.
"""

from __future__ import annotations

from chatcopilot.evals.models import EvalCase, JudgeResult


def load_cases(limit: int | None = None) -> tuple[EvalCase, ...]:
    """WebArena requires a self-hosted web environment; not yet implemented."""

    return ()


def prepare_task(case: EvalCase) -> None:
    raise NotImplementedError(
        "WebArena 需要 Agent 浏览器操作工具 + 自建 WebArena Web 应用集群。"
    )


def judge(case: EvalCase, actions: list[dict]) -> JudgeResult:
    raise NotImplementedError(
        "WebArena 判分需要浏览器环境中的任务完成检测。"
    )


__all__ = ["judge", "load_cases", "prepare_task"]
