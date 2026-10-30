"""Human-operated external software installation commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.model_settings import ModelSettingsError, read_settings, resolve_binding, settings_path
from chatcopilot.plugins.catalog import PLUGINS, default_root
from chatcopilot.plugins.repository import InstallRepository
from chatcopilot.deployment.systemd import require_platform
from chatcopilot.plugins.runtime import create_manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentstrata plugin")
    parser.add_argument("operation", choices=("list", "install", "doctor", "start", "stop", "uninstall"))
    parser.add_argument("plugin", nargs="?")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--version")
    parser.add_argument("--artifacts", type=Path, help="Local verified release bundle (development/offline distribution)")
    parser.add_argument("--source-url", default=os.environ.get("CHATCOPILOT_SOURCE_URL", ""))
    parser.add_argument("--model-binding")
    parser.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                        help="Plugin-specific option; value may be a JSON scalar")
    parser.add_argument("--no-start", action="store_true", help="Prepare only; normal installation starts services")
    args = parser.parse_args(argv)
    if args.operation != "list" and not args.plugin:
        parser.error("a plugin id is required")
    manager = create_manager(args.root)
    try:
        if args.operation == "list":
            result = manager.list()
        elif args.operation == "install":
            require_platform()
            if args.plugin not in PLUGINS:
                raise InstallError("Unknown plugin; use 'agentstrata plugin list'")
            binding = args.model_binding
            repository = InstallRepository(args.root, args.plugin)
            existing = repository.read()
            needs_binding = (PLUGINS[args.plugin].requires_agent
                             and not (existing and (repository.root / "config/request.json").exists()))
            document = read_settings() if needs_binding else {}
            if needs_binding and not binding:
                choices = sorted(document["bindings"])
                if not sys.stdin.isatty():
                    raise InstallError("Pass --model-binding for non-interactive installation")
                print("Available model bindings:\n" + "\n".join(choices), file=sys.stderr)
                binding = input("Model binding: ").strip()
            if needs_binding and binding:
                # Services do not inherit this terminal's model credentials.
                model = resolve_binding(binding, document=document, environment={})
                if ((model.auth_mode == "api_key" and not model.api_key)
                        or (model.auth_mode == "chatgpt" and not model.credential_root)):
                    raise InstallError("Model connection must reference available credentials through its env_file")
            options = {}
            for option in args.option:
                key, separator, value = option.partition("=")
                if not separator or not key:
                    raise InstallError("Plugin options use KEY=VALUE")
                try:
                    options[key] = json.loads(value)
                except ValueError:
                    options[key] = value
            result = manager.install(args.plugin, selected_version=args.version,
                artifacts_directory=args.artifacts, start=not args.no_start,
                options=options,
                host={"python": sys.executable, "model_config": str(settings_path()),
                      "model_binding": binding, "source_url": args.source_url})
        else:
            result = getattr(manager, args.operation)(args.plugin)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (InstallError, ModelSettingsError, OSError, ValueError) as exc:
        print(f"Plugin operation failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Plugin operation interrupted; installation progress is retained", file=sys.stderr)
        return 130
