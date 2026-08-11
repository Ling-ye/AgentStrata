#!/usr/bin/env python3
"""Resolve Docker shared-service desired state from enabled BotSpecs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path


SERVICE_ORDER = ("searxng", "playwright-mcp", "xiaohongshu-mcp")
MCP_SERVICE_BY_CATALOG_REF = {
    "playwright-browser": "playwright-mcp",
    "xiaohongshu-search": "xiaohongshu-mcp",
}


class DesiredStateError(ValueError):
    """Raised when desired state cannot be resolved safely."""


def _explicit_bot_specs(raw: str) -> tuple[Path, ...]:
    return tuple(
        Path(item).expanduser().resolve()
        for item in raw.split(os.pathsep)
        if item.strip()
    )


def discover_bot_specs(repo_root: Path, explicit: Iterable[Path] = ()) -> tuple[Path, ...]:
    requested = tuple(Path(path).expanduser().resolve() for path in explicit)
    if requested:
        paths = requested
    else:
        env_paths = _explicit_bot_specs(os.environ.get("CHATCOPILOT_BOT_SPECS", ""))
        single = os.environ.get("CHATCOPILOT_BOT_SPEC", "").strip()
        paths = env_paths + ((Path(single).expanduser().resolve(),) if single else ())
        if not paths:
            paths = tuple(sorted((repo_root / "bots").glob("*/bot.yaml")))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise DesiredStateError(
            "BotSpec file does not exist: " + ", ".join(str(path) for path in missing)
        )
    discovered = tuple(dict.fromkeys(path.resolve() for path in paths))
    if not discovered:
        raise DesiredStateError(
            "no BotSpec files were discovered; refusing to reconcile Docker services"
        )
    return discovered


def _load_validated_runtime_projection(bot_path: Path):
    try:
        source_root = Path(__file__).resolve().parents[2] / "src"
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        from chatcopilot.botspec.loader import load_botspec, validate_botspec
        from chatcopilot.botspec.mcp import load_mcp_server_configs

        spec = load_botspec(bot_path)
        issues = validate_botspec(spec)
    except Exception as exc:
        raise DesiredStateError(f"{bot_path}: BotSpec loading failed: {exc}") from exc
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        detail = "; ".join(
            f"{issue.field or '<root>'}: {issue.message}" for issue in errors
        )
        raise DesiredStateError(f"{bot_path}: BotSpec validation failed: {detail}")
    try:
        mcp_servers = load_mcp_server_configs(spec)
    except Exception as exc:
        raise DesiredStateError(
            f"{bot_path}: MCP runtime projection failed: {exc}"
        ) from exc
    return spec, mcp_servers


def resolve_desired_services(bot_paths: Iterable[Path]) -> tuple[str, ...]:
    paths = tuple(bot_paths)
    if not paths:
        raise DesiredStateError(
            "at least one valid BotSpec is required before Docker reconciliation"
        )
    desired: set[str] = set()
    for raw_path in paths:
        bot_path = Path(raw_path).expanduser().resolve()
        spec, mcp_servers = _load_validated_runtime_projection(bot_path)
        if spec.agents.research_enabled and any(
            provider.enabled and provider.kind == "searxng"
            for provider in spec.agents.search_providers
        ):
            desired.add("searxng")
        for server in mcp_servers:
            service = MCP_SERVICE_BY_CATALOG_REF.get(server.catalog_ref)
            if service:
                desired.add(service)
    return tuple(service for service in SERVICE_ORDER if service in desired)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--bot-spec", action="append", type=Path, default=[])
    parser.add_argument("--format", choices=("lines", "json"), default="lines")
    args = parser.parse_args(argv)
    try:
        paths = discover_bot_specs(args.repo_root.resolve(), args.bot_spec)
        services = resolve_desired_services(paths)
    except DesiredStateError as exc:
        print(f"desired-state error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps({"bot_specs": [str(path) for path in paths], "services": services}))
    else:
        print("\n".join(services))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
