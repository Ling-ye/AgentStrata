#!/usr/bin/env python3
"""Audit AgentStrata's packaged Component Catalog and runtime projection."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chatcopilot.component_catalog import audit_component_catalog  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one deterministic JSON document",
    )
    args = parser.parse_args(argv)

    report = audit_component_catalog()
    if args.json:
        print(
            json.dumps(
                report.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    elif report.ok:
        stats = report.stats
        print(
            "OK: component catalog "
            f"({stats.tool_packs} packs, {stats.static_tools} static tools, "
            f"{stats.mcp_entries} MCP entries, {stats.subagents} subagents, "
            f"{stats.workflows} workflows)"
        )
    else:
        print(
            f"Component catalog audit failed with {len(report.issues)} issue(s):",
            file=sys.stderr,
        )
        for issue in report.issues:
            locations = [
                value
                for value in (
                    issue.surface,
                    issue.component,
                    issue.module,
                    issue.tool,
                )
                if value
            ]
            location = ":".join(locations)
            print(
                f"[{issue.code}] {location}: {issue.message}",
                file=sys.stderr,
            )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
