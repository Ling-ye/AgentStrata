"""Local single-case repair controls. No daemon or Console process is required."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.models import RepairFeedback, RepairOptions


def _governance_options(args, default_model):
    from chatcopilot.harness.governance_types import GovernanceOptions
    stop = ({"mode": "findings", "count": args.findings} if args.findings is not None else
            {"mode": "time", "seconds": args.timeout_seconds if args.timeout_seconds is not None else 3600})
    return GovernanceOptions(args.model or default_model, args.reasoning_effort, args.max_attempts, stop)


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
    start.add_argument("--timeout-seconds", type=int, default=3600)
    start.add_argument("--request-id")
    start.add_argument("--repair-hint", default="", help="调查线索，不覆盖 Case 评分")
    task_start = sub.add_parser("start-task", parents=[], help="从实例 Gateway 任务记录发起修复")
    task_start.add_argument("--bot", required=True)
    task_start.add_argument("--run", required=True)
    task_start.add_argument("--gateway-state-root", type=Path, required=True)
    task_start.add_argument("--model", default="")
    task_start.add_argument("--reasoning-effort", default="medium")
    task_start.add_argument("--max-attempts", type=int, default=3)
    task_start.add_argument("--timeout-seconds", type=int, default=3600)
    task_start.add_argument("--request-id")
    task_start.add_argument("--repair-hint", default="", help="调查线索或修复提示")
    task_start.add_argument("--expected-behavior", default="", help="参考答案或预期行为")
    maintenance = sub.add_parser("maintenance", help="证明 Harness 空闲并在命令期间阻止创建和恢复任务")
    maintenance.add_argument("argv", nargs=argparse.REMAINDER)
    reset = sub.add_parser("cutover", help="确认旧任务空闲并归档；默认只检查")
    reset.add_argument("--apply", action="store_true", help="归档旧任务并启用空的新协议记录")
    gc = sub.add_parser("start-gc", help="依据 SDD 与黄金原则启动代码熵回收")
    gc.add_argument("--model", default="")
    gc.add_argument("--reasoning-effort", default="medium")
    gc.add_argument("--max-attempts", type=int, default=3)
    gc_stop = gc.add_mutually_exclusive_group()
    gc_stop.add_argument("--timeout-seconds", type=int, help="整次回收累计执行秒数；默认 3600")
    gc_stop.add_argument("--findings", type=int, help="问题发现数上限；逐项修复并合并后继续")
    gc.add_argument("--request-id")
    sub.add_parser("gc-tick", help="执行一次已启用的熵回收定时触发")
    schedule = sub.add_parser("gc-schedule", help="查看或配置熵回收定时；默认关闭")
    enabled = schedule.add_mutually_exclusive_group()
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    schedule.add_argument("--interval-hours", type=int, default=24)
    schedule.add_argument("--model", default="")
    schedule.add_argument("--reasoning-effort", default="medium")
    schedule.add_argument("--max-attempts", type=int, default=3)
    schedule_stop = schedule.add_mutually_exclusive_group()
    schedule_stop.add_argument("--timeout-seconds", type=int)
    schedule_stop.add_argument("--findings", type=int)
    for name in ("get-gc", "cancel-gc", "resume-gc"):
        sub.add_parser(name).add_argument("run")
    listing = sub.add_parser("list")
    listing.add_argument("--page", type=int, default=1)
    listing.add_argument("--search", default="")
    listing.add_argument("--status", default="")
    for name in ("get", "cancel", "resume", "reconcile", "retry-delivery", "retry-cleanup"):
        sub.add_parser(name).add_argument("task")
    args = parser.parse_args(argv)
    value: Any
    try:
        controller = HarnessController(
            args.repository_root,
            root=args.root,
            gateway_state_root=args.gateway_state_root if args.command == "start-task" else None,
        )
        if args.command == "maintenance":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            return controller.maintenance(command)
        if args.command == "cutover":
            value = controller.cutover(apply=args.apply)
        elif args.command == "start-gc":
            value = controller.start_code_health(_governance_options(args, controller.default_model), request_id=args.request_id)
        elif args.command in {"get-gc", "cancel-gc", "resume-gc"}:
            action = {"get-gc": controller.governance_run, "cancel-gc": controller.cancel_governance_run,
                      "resume-gc": controller.resume_governance_run}[args.command]
            value = action(args.run)
        elif args.command == "gc-tick":
            value = controller.governance_tick()
        elif args.command == "gc-schedule":
            from chatcopilot.harness.governance_types import GovernanceSchedule
            if not args.enable and not args.disable:
                value = controller.governance_schedule()
            else:
                options = _governance_options(args, controller.default_model) if args.enable else None
                value = controller.set_governance_schedule(GovernanceSchedule(args.enable, args.interval_hours, options))
        elif args.command == "start":
            value = controller.start(
                args.evaluation,
                args.case,
                args.target,
                RepairOptions(
                    args.model or controller.default_model,
                    args.reasoning_effort,
                    args.max_attempts,
                    args.timeout_seconds,
                ),
                request_id=args.request_id,
                feedback=RepairFeedback(repair_hint=args.repair_hint),
            )
        elif args.command == "start-task":
            value = controller.start_task(
                args.bot,
                args.run,
                RepairOptions(
                    args.model or controller.default_model,
                    args.reasoning_effort,
                    args.max_attempts,
                    args.timeout_seconds,
                ),
                request_id=args.request_id,
                feedback=RepairFeedback(args.repair_hint, args.expected_behavior),
            )
        elif args.command == "list":
            value = controller.list(page=args.page, search=args.search, status=args.status)
        else:
            value = getattr(controller, args.command.replace("-", "_"))(args.task)
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
