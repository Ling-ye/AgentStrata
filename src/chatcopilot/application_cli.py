"""Application CLI composition using the existing installation and Harness controls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from chatcopilot.core.application_installation import installation
from chatcopilot.deployment.application_bundle import read_bundle
from chatcopilot.deployment.application_runtime import install_bundle, prepare_runtime, setup_application


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agentstrata app")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Verify an independent application bundle")
    inspect.add_argument("bundle", type=Path)
    install = commands.add_parser("install", help="Install a new application source copy without overwriting an existing one")
    install.add_argument("bundle", type=Path)
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--source-only", action="store_true", help="Materialize verified sources without installing dependencies")
    setup = commands.add_parser("setup", help="Configure and start this application's existing runtime")
    setup.add_argument("--root", type=Path, required=True)
    setup.add_argument("--with-console", action="store_true")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--artifacts", type=Path, help="Verified local host/plugin wheels for application components")
    status = commands.add_parser("status", help="Read the installed application and selected source version")
    status.add_argument("--root", type=Path, required=True)
    desktop = commands.add_parser("desktop-info", help="Private connection handoff to the local native desktop process")
    desktop.add_argument("--root", type=Path, required=True)
    for name in ("start", "stop"):
        action = commands.add_parser(name, help="Control the registered services of this installation")
        action.add_argument("--root", type=Path, required=True)
    for name in ("update", "rollback"):
        action = commands.add_parser(name, help="Use the existing accepted-candidate activation controls")
        action.add_argument("task")
        action.add_argument("--root", type=Path, required=True)
        action.add_argument("--harness-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            record = read_bundle(args.bundle)
            value = {key: record[key] for key in ("schema", "bot_id", "bot_relative", "source_digest", "accepted_candidate")}
            value["source_files"] = len(record["source_manifest"])
        elif args.command == "install":
            value = install_bundle(args.bundle, args.root)
            if not args.source_only:
                code = prepare_runtime(args.root)
                if code:
                    print(json.dumps({**value, "state": "runtime_installation_failed", "exit_code": code}, ensure_ascii=False))
                    return code
                value["state"] = "needs_configuration"
        elif args.command == "setup":
            installed = installation(args.root)
            if installed is None:
                raise ValueError("An explicit application installation is required")
            if (installed.control / "current.json").exists() or (installed.control / "current.json").is_symlink():
                raise ValueError("Withdraw the managed application version before setup")
            if not args.dry_run:
                from chatcopilot.plugins.application_components import prepare_configuration, prepare_components
                requirements = prepare_configuration(args.root)
                if requirements:
                    code = subprocess.run([str(args.root / ".venv/bin/python"), "-m", "chatcopilot", "bot", "provision-env",
                        "--bot", str(args.root / installed.bot_relative)], cwd=args.root, check=False).returncode
                    if code:
                        return code
                    prepare_components(args.root, requirements, artifacts=args.artifacts)
            return setup_application(args.root, with_console=args.with_console, dry_run=args.dry_run)
        elif args.command == "status":
            from chatcopilot.deployment.application_services import status as application_status
            value = application_status(args.root)
        elif args.command == "desktop-info":
            from chatcopilot.deployment.application_services import desktop_info
            value = desktop_info(args.root)
        elif args.command in {"start", "stop"}:
            from chatcopilot.deployment.application_services import control
            value = control(args.root, args.command)
        else:
            if installation(args.root) is None:
                raise ValueError("No independent application is installed here")
            from chatcopilot.harness.api import HarnessController
            controller = HarnessController(args.root, root=args.harness_root)
            value = controller.activate(args.task) if args.command == "update" else controller.rollback_activation(args.task)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        if args.command in {"update", "rollback"}:
            return 0 if value.get("state") == ("active" if args.command == "update" else "rolled_back") else 1
        if args.command in {"start", "stop"}:
            return 0 if value.get("ok") is True else 1
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Application operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
