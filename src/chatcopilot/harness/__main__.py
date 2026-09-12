"""Local single-case repair controls. No daemon or Console process is required."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.models import RepairFeedback, RepairOptions
from chatcopilot.harness.config import configuration
from chatcopilot.harness.gateway_adapter import read_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--evaluation", required=True)
    start.add_argument("--case", required=True)
    start.add_argument("--target", required=True)
    start.add_argument("--model", default="")
    start.add_argument("--reasoning-effort", default="medium")
    start.add_argument("--max-attempts", type=int, default=3)
    start.add_argument("--timeout-seconds", type=int, default=7200)
    start.add_argument("--request-id")
    start.add_argument(
        "--review-and-commit", action="store_true", help="AI 审核通过后在任务分支本地提交"
    )
    task_start = sub.add_parser("start-task", parents=[], help="从实例 Gateway 任务记录发起修复")
    task_start.add_argument("--bot", required=True)
    task_start.add_argument("--run", required=True)
    task_start.add_argument("--gateway-state-root", type=Path, required=True)
    task_start.add_argument("--model", default="")
    task_start.add_argument("--reasoning-effort", default="medium")
    task_start.add_argument("--max-attempts", type=int, default=3)
    task_start.add_argument("--timeout-seconds", type=int, default=7200)
    task_start.add_argument("--request-id")
    task_start.add_argument("--repair-hint", default="", help="调查线索或修复提示")
    task_start.add_argument("--expected-behavior", default="", help="参考答案或预期行为")
    task_start.add_argument(
        "--review-and-commit", action="store_true", help="AI 审核通过后本地提交修复与回归测试"
    )
    listing = sub.add_parser("list")
    listing.add_argument("--page", type=int, default=1)
    listing.add_argument("--search", default="")
    listing.add_argument("--status", default="")
    for name in ("get", "cancel", "resume"):
        sub.add_parser(name).add_argument("task")
    args = parser.parse_args(argv)
    value: Any
    try:
        controller = HarnessController(
            args.repository_root,
            root=args.root,
            task_reader=(lambda bot, run: read_task(args.gateway_state_root, bot, run))
            if args.command == "start-task"
            else None,
        )
        if args.command == "start":
            value = controller.start(
                args.evaluation,
                args.case,
                args.target,
                RepairOptions(
                    args.model or configuration().get("CHATCOPILOT_HARNESS_MODEL", ""),
                    args.reasoning_effort,
                    args.max_attempts,
                    args.timeout_seconds,
                ),
                request_id=args.request_id,
                review_and_commit=args.review_and_commit,
            )
        elif args.command == "start-task":
            value = controller.start_task(
                args.bot,
                args.run,
                RepairOptions(
                    args.model or configuration().get("CHATCOPILOT_HARNESS_MODEL", ""),
                    args.reasoning_effort,
                    args.max_attempts,
                    args.timeout_seconds,
                ),
                request_id=args.request_id,
                review_and_commit=args.review_and_commit,
                feedback=RepairFeedback(args.repair_hint, args.expected_behavior),
            )
        elif args.command == "list":
            value = controller.list(page=args.page, search=args.search, status=args.status)
        else:
            value = getattr(controller, args.command)(args.task)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"error": getattr(exc, "code", "invalid_request"), "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
