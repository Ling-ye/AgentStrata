"""BotSpec command line utilities."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from chatcopilot.botspec.loader import load_botspec, validate_botspec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chatcopilot.botspec",
        description="Validate and inspect AgentStrata BotSpec files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a bot.yaml file.")
    validate_parser.add_argument("path", type=str, help="Path to bots/<bot-id>/bot.yaml")
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="Print validation result as JSON.",
    )

    show_parser = subparsers.add_parser("show", help="Print a normalized BotSpec.")
    show_parser.add_argument("path", type=str, help="Path to bots/<bot-id>/bot.yaml")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate(Path(args.path), json_output=args.json)
    if args.command == "show":
        return _show(Path(args.path))
    parser.error(f"unknown command: {args.command}")
    return 2


def _validate(path: Path, *, json_output: bool) -> int:
    spec = load_botspec(path)
    issues = validate_botspec(spec)
    has_error = any(issue.level == "error" for issue in issues)

    if json_output:
        print(
            json.dumps(
                {
                    "ok": not has_error,
                    "bot_id": spec.id,
                    "issues": [asdict(issue) for issue in issues],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if has_error else 0

    if not issues:
        print(f"OK: {spec.id} ({spec.display_name})")
        return 0

    for issue in issues:
        field = f" [{issue.field}]" if issue.field else ""
        print(f"{issue.level.upper()}{field}: {issue.message}")
    return 1 if has_error else 0


def _show(path: Path) -> int:
    spec = load_botspec(path)
    data = asdict(spec)
    data["source_path"] = str(spec.source_path)
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
