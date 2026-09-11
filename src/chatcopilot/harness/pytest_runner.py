"""Isolated pytest reporter; executed as a script without importing candidate Harness code."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


def main() -> int:
    os.umask(0o077)
    request = json.loads(Path(sys.argv[1]).read_text())
    output = Path(sys.argv[2])
    rows: dict[str, dict] = {}
    collected: list[str] = []
    errors: list[str] = []

    class Reporter:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            report.harness_assertion_failure = bool(
                call.excinfo and call.excinfo.errisinstance(AssertionError)
            )

        def pytest_collection_modifyitems(self, items):
            collected.extend(item.nodeid for item in items)
            if request.get("selected") is not None:
                wanted = set(request["selected"])
                items[:] = [item for item in items if item.nodeid in wanted]

        def pytest_collectreport(self, report):
            if report.failed:
                errors.append(str(report.longrepr)[-6000:])

        def pytest_runtest_logreport(self, report):
            row = rows.setdefault(report.nodeid, {"outcome": "not_run", "phases": []})
            row["phases"].append({"when": report.when, "outcome": report.outcome})
            if report.when == "call" and row["outcome"] not in {"failed", "error"}:
                row.update(
                    outcome=report.outcome,
                    message=str(report.longrepr)[-6000:] if report.failed else "",
                )
                row["assertion_failure"] = report.failed and getattr(
                    report, "harness_assertion_failure", False
                )
            elif report.failed:
                row.update(outcome="error", message=str(report.longrepr)[-6000:])
            elif report.skipped:
                row["outcome"] = "skipped"
            if hasattr(report, "wasxfail"):
                row["outcome"] = "skipped"

    args = [
        *request["paths"],
        "--rootdir=" + request["root"],
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
    ]
    if request.get("collect"):
        args.append("--collect-only")
    code = int(pytest.main(args, plugins=[Reporter()]))
    output.write_text(
        json.dumps({"exit_code": code, "collected": collected, "rows": rows, "errors": errors})
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
