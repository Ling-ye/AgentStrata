#!/usr/bin/env python3
"""Render check results and JUnit identities, never raw logs or failure bodies."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET


def _cell(value: object) -> str:
    return html.escape(str(value)).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render(report_dir: Path, title: str) -> str:
    lines = [f"## {_cell(title)}", ""]
    try:
        manifest = json.loads((report_dir / "manifest.json").read_text(encoding="utf-8"))
        checks = manifest["checks"]
        if not isinstance(checks, list):
            raise ValueError("checks must be a list")
    except (OSError, ValueError, KeyError, TypeError):
        return "\n".join([*lines, "检查记录未生成或不可读取；请查看前置步骤。结果未标记为通过。", ""])
    status = "passed" if manifest.get("ok") is True else "failed" if manifest.get("finished_at") else "incomplete"
    lines.extend([f"Overall: **{status}**", "", "| Check | Status | Seconds |", "| --- | --- | --- |"])
    for check in checks:
        lines.append("| " + " | ".join(_cell(check.get(key, "")) for key in
                                      ("name", "status", "elapsed_seconds")) + " |")
    failed = sorted({node for check in checks for node in check.get("failed_ids", [])} |
                    {node for check in checks for node in check.get("test_inventory", {}).get("error_ids", [])})
    if failed:
        lines.extend(["", "### Failed test identities", "", *(f"- {_cell(node)}" for node in failed)])
    skipped = {node: "reason unavailable" for check in checks
               for node in check.get("test_inventory", {}).get("skipped_ids", [])}
    for index, check in enumerate(checks, start=1):
        if "test_inventory" not in check:
            continue
        report = report_dir / f"{index:02d}-pytest.xml"
        try:
            for case in ET.parse(report).iter("testcase"):
                if (skip := case.find("skipped")) is not None:
                    node = case.get("classname", "") + "::" + case.get("name", "")
                    skipped[node] = skip.get("message") or skip.text or "reason unavailable"
        except (OSError, ET.ParseError):
            lines.extend(["", "JUnit 跳过原因不可读取；保留 manifest 中的跳过清单。"])
    lines.extend(["", f"### Skipped tests ({len(skipped)})", ""])
    lines.extend(f"- {_cell(node)} — {_cell(reason)}" for node, reason in sorted(skipped.items()))
    if not skipped:
        lines.append("No skipped test identities recorded.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--title", default="Repository checks")
    parser.add_argument("--output", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)
    summary = render(args.report_dir, args.title)
    if args.output:
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(summary)
    else:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
