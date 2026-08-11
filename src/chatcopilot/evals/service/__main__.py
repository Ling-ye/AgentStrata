"""Command line entry for the local Evaluation service."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import uuid
from pathlib import Path

from chatcopilot.core.logging import configure_logging
from chatcopilot.evals.service.client import EvaluationServiceClient
from chatcopilot.evals.service.protocol import default_socket_path
from chatcopilot.evals.service.server import EvaluationUnixServer, build_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatcopilot.evals.service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run the local Evaluation service.")
    serve.add_argument("--socket", type=Path, default=None)
    serve.add_argument("--repository-root", type=Path, default=None)
    serve.add_argument("--artifact-root", type=Path, default=None)
    health = subparsers.add_parser("health", help="Query the local service.")
    health.add_argument("--socket", type=Path, default=None)
    health.add_argument("--json", action="store_true")
    health.add_argument(
        "--require-idle",
        action="store_true",
        help="Return non-zero while any Evaluation is queued or running.",
    )
    maintenance = subparsers.add_parser(
        "maintenance",
        help="Hold or release the update maintenance lease.",
    )
    maintenance_subparsers = maintenance.add_subparsers(
        dest="maintenance_command",
        required=True,
    )
    maintenance_enter = maintenance_subparsers.add_parser(
        "enter",
        help="Atomically prove idle and block new Evaluations.",
    )
    maintenance_enter.add_argument("--socket", type=Path, default=None)
    maintenance_enter.add_argument("--lease-id", default=None)
    maintenance_leave = maintenance_subparsers.add_parser(
        "leave",
        help="Release a previously acquired maintenance lease.",
    )
    maintenance_leave.add_argument("--socket", type=Path, default=None)
    maintenance_leave.add_argument("--lease-id", required=True)
    maintenance_status = maintenance_subparsers.add_parser(
        "status",
        help="Show the current maintenance lease.",
    )
    maintenance_status.add_argument("--socket", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "maintenance":
        return _maintenance(args)
    return _health(args)


def _serve(args: argparse.Namespace) -> int:
    configure_logging("INFO", "CHATCOPILOT_EVALUATION_LOG_LEVEL")
    repository_root = args.repository_root or Path(
        os.environ.get("CHATCOPILOT_SOURCE_ROOT", Path.cwd())
    )
    artifact_value = args.artifact_root or os.environ.get("CHATCOPILOT_EVALUATION_ROOT")
    artifact_root = Path(artifact_value) if artifact_value else None
    runtime = build_runtime(
        repository_root=repository_root,
        artifact_root=artifact_root,
    )
    server = EvaluationUnixServer(
        args.socket or default_socket_path(),
        runtime,
    )
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop)
    try:
        server.serve(stopped)
    finally:
        server.close()
    return 0


def _health(args: argparse.Namespace) -> int:
    try:
        payload = EvaluationServiceClient(args.socket).health()
    except Exception as exc:  # noqa: BLE001 - health command boundary
        print(f"Evaluation service unavailable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Evaluation service: "
            f"ready={str(bool(payload.get('ready'))).lower()} "
            f"active_count={int(payload.get('active_count') or 0)} "
            f"idle_proven={str(payload.get('idle_proven') is True).lower()}"
        )
    if payload.get("ready") is not True:
        return 1
    if args.require_idle and payload.get("idle_proven") is not True:
        if int(payload.get("active_count") or 0) > 0:
            message = "Evaluation service has active work; update refused"
        else:
            message = "Evaluation service cannot prove idle state; update refused"
        print(message, file=sys.stderr)
        return 2
    return 0


def _maintenance(args: argparse.Namespace) -> int:
    client = EvaluationServiceClient(args.socket)
    try:
        if args.maintenance_command == "status":
            print(
                json.dumps(
                    client.maintenance_status(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.maintenance_command == "enter":
            lease_id = str(args.lease_id or uuid.uuid4().hex)
            payload = client.enter_maintenance(lease_id)
            print(str(payload.get("lease_id") or lease_id))
            return 0
        payload = client.leave_maintenance(str(args.lease_id))
    except Exception as exc:  # noqa: BLE001 - maintenance command boundary
        print(f"Evaluation maintenance request failed: {exc}", file=sys.stderr)
        return 1
    if payload.get("maintenance") is True:
        print("Evaluation maintenance lease was not released", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
