"""python -m console.control — 控制契约的命令行入口。

便于在没有后端/前端的情况下，直接从 WSL 终端或别的脚本复用同一套控制逻辑。

示例：
  python -m console.control list --json
  python -m console.control status --instance lingye-copilot-qq --json
  python -m console.control restart --instance lingye-copilot-qq
  python -m console.control sync --instance lingye-copilot-qq
  python -m console.control jobs --instance lingye-copilot-qq --json
  python -m console.control logs --instance lingye-copilot-qq --source cc
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from console.bootstrap import ensure_src_path

ensure_src_path()

from console.control import operations  # noqa: E402
from console.control.discovery import discover_instances, find_instance  # noqa: E402
from console.control.instances import BotInstance  # noqa: E402


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _require(instance_id: Optional[str]) -> BotInstance:
    if not instance_id:
        print("[ERR] 需要 --instance <id>", file=sys.stderr)
        raise SystemExit(2)
    inst = find_instance(instance_id)
    if inst is None:
        print(f"[ERR] 找不到实例：{instance_id}", file=sys.stderr)
        raise SystemExit(1)
    return inst


def _stream(it) -> int:
    rc = 0
    for line in it:
        if line.startswith("__EXIT__"):
            rc = int(line.split()[1])
            continue
        print(line, flush=True)
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m console.control")
    parser.add_argument("action", choices=[
        "list", "status", "start", "stop", "restart",
        "sync", "rebuild", "update", "dump", "jobs", "logs",
        "diagnose",
    ])
    parser.add_argument("--instance", "-i", default=None)
    parser.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    parser.add_argument("--dry-run", action="store_true", help="sync/update 时只预览")
    parser.add_argument("--mode", default="quick", help="dump 模式：quick|full")
    parser.add_argument("--source", default="cc", help="logs 来源：cc|questions|runtime")
    parser.add_argument("--lines", type=int, default=200, help="logs tail 行数")
    parser.add_argument("--id", dest="query_id", default=None, help="task_* 或 job_* ID")
    parser.add_argument("--out", default=None, help="诊断证据包输出目录")
    args = parser.parse_args(argv)

    if args.action == "list":
        _print([i.to_dict() for i in discover_instances()], args.json)
        return 0

    if args.action == "status":
        inst = _require(args.instance)
        _print(operations.status(inst), args.json)
        return 0

    if args.action in {"start", "stop", "restart"}:
        inst = _require(args.instance)
        res = operations.control(inst, args.action)
        _print(res, args.json)
        return 0 if res.get("ok") else 1

    if args.action == "sync":
        inst = _require(args.instance)
        return _stream(operations.stream_sync(inst, dry_run=args.dry_run))

    if args.action == "rebuild":
        inst = _require(args.instance)
        return _stream(operations.stream_rebuild(inst))

    if args.action == "update":
        inst = _require(args.instance)
        return _stream(operations.stream_update(inst, dry_run=args.dry_run))

    if args.action == "dump":
        inst = _require(args.instance)
        return _stream(operations.stream_dump(inst, mode=args.mode))

    if args.action == "jobs":
        inst = _require(args.instance)
        _print(operations.jobs(inst), args.json)
        return 0

    if args.action == "logs":
        inst = _require(args.instance)
        for path in operations.resolve_log_files(inst, args.source):
            for line in operations.tail_log(path, args.lines):
                print(line)
        return 0

    if args.action == "diagnose":
        if not args.query_id or not args.out:
            parser.error("diagnose 需要 --id <task_or_job_id> --out <dir>")
        from console.control.diagnostics import DiagnosticError, collect_diagnostic_bundle
        try:
            result = collect_diagnostic_bundle(args.query_id, Path(args.out))
        except DiagnosticError as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1
        _print(result, args.json)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
