"""Application CLI composition using the existing installation and Harness controls."""
from __future__ import annotations

import argparse
import json
import os
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
    run = commands.add_parser("run", help="Run an installed native application in the foreground")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--watch-stdin", action="store_true", help="Stop when the owning desktop closes its pipe")
    models = commands.add_parser("model-settings", help="Read or save this application's shared model configuration")
    models.add_argument("--root", type=Path, required=True)
    models.add_argument("--save", action="store_true", help="Read configuration from stdin; credentials are never argv")
    login = commands.add_parser("login", help="Explicitly authenticate this application's ChatGPT model connection")
    login.add_argument("--root", type=Path, required=True)
    login.add_argument("--method", choices=("browser", "device"), default="browser")
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
            value = {key: record[key] for key in ("schema", "bot_id", "bot_relative", "source_digest", "accepted_candidate", "origin") if key in record}
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
            if os.name == "nt":
                from chatcopilot.plugins.windows_application import setup
                value = _setup_windows(args, setup)
                print(json.dumps(value, ensure_ascii=False))
                return 0
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
        elif args.command == "run":
            if os.name != "nt":
                raise ValueError("Foreground app run currently targets native Windows; use app start on Linux")
            from chatcopilot.deployment.windows_runtime import run as run_application
            return run_application(args.root, watch_stdin=args.watch_stdin)
        elif args.command == "model-settings":
            from chatcopilot.deployment.application_models import model_settings
            value = model_settings(args.root, json.load(sys.stdin) if args.save else None)
        elif args.command == "login":
            from chatcopilot.external_tools.codex_cli.auth_cli import CodexAuthOperatorConfig, login_lanes
            from chatcopilot.core.settings import load_local_env_values
            installed = installation(args.root)
            if installed is None:
                raise ValueError("Authentication requires an installed application")
            environment = load_local_env_values(installed.control / "runtime.env", expand_home=True)
            config = CodexAuthOperatorConfig.from_values(environment["CHATCOPILOT_CODEX_BOT_HOME"],
                                                        environment["CHATCOPILOT_CODEX_BIN"])
            value = login_lanes(config, "main", method=args.method)[0].to_dict()
            print(json.dumps(value, ensure_ascii=False))
            return 0 if value["ok"] else 1
        elif args.command in {"start", "stop"}:
            from chatcopilot.deployment.application_services import control
            value = control(args.root, args.command)
        else:
            if os.name == "nt":
                raise ValueError("Windows accepted-version activation and rollback are outside this runtime release")
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


def _setup_windows(args, prepare_plugins):
    if args.dry_run:
        return prepare_plugins(args.root, artifacts=args.artifacts, dry_run=True)
    from chatcopilot.botspec import load_botspec
    from chatcopilot.botspec.deployment_env import deployment_environment
    from chatcopilot.core.install_artifacts import private_directory, write_private
    from chatcopilot.core.settings import load_local_env_values
    from chatcopilot.plugins.application_components import prepare_configuration
    root = args.root
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit application installation is required")
    local = root / Path(installed.bot_relative).parent / "local.env"
    if not local.exists() and not local.is_symlink():
        write_private(local, "")
    requirements = prepare_configuration(root)
    spec = load_botspec(root / installed.bot_relative)
    values = load_local_env_values(root / Path(installed.bot_relative).parent / "local.env", expand_home=True)
    for requirement in requirements:
        if requirement["id"] == "open-llm-vtuber":
            values[spec.gateway.port_env] = str(requirement["options"]["gateway_port"])
    control = private_directory(installed.control)
    values.update(AGENTSTRATA_LLM_CONFIG=str(control / "llm.json"),
        CHATCOPILOT_GATEWAY_STATE_ROOT=str(root / ".agentstrata-data/gateway"),
        AGENTSTRATA_WINDOWS_MCP_PYTHON=str(root / "windows-mcp/venv/Scripts/python.exe"),
        CHATCOPILOT_CODEX_BOT_HOME=str(control / "model-auth"),
        CHATCOPILOT_CODEX_BIN=str(root / "runtime/tools/codex/codex.exe"),
        PATH=str(root / ".venv/Scripts") + os.pathsep + os.environ.get("PATH", ""))
    private_directory(root / ".agentstrata-data/workspace")
    private_directory(root / ".agentstrata-data/gateway")
    environment = deployment_environment(spec, values, source_root=root, home=Path.home())
    write_private(control / "runtime.env", "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
                                                   for key, value in environment.items()) + "\n")
    return prepare_plugins(root, artifacts=args.artifacts, requirements=requirements)


if __name__ == "__main__":
    raise SystemExit(main())
