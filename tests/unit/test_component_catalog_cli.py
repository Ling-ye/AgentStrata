from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from chatcopilot.component_catalog.audit import (
    CatalogAuditIssue,
    CatalogAuditReport,
    CatalogAuditStats,
)


ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    path = ROOT / "scripts" / "check_component_catalog.py"
    spec = importlib.util.spec_from_file_location("check_component_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_component_catalog_cli_json_contract(capsys) -> None:
    script = _load_script()

    assert script.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["issue_count"] == 0
    assert payload["stats"]["static_tools"] > 0


def test_component_catalog_cli_returns_nonzero_for_structured_issues(
    monkeypatch,
    capsys,
) -> None:
    script = _load_script()
    report = CatalogAuditReport(
        issues=(
            CatalogAuditIssue(
                code="tool_binding.tool_missing",
                message="A declared tool is missing.",
                surface="tool_pack",
                component="tests.pack",
                module="chatcopilot.external_tools.tests.tools",
                tool="missing_tool",
            ),
        ),
        stats=CatalogAuditStats(),
    )
    monkeypatch.setattr(script, "audit_component_catalog", lambda: report)

    assert script.main([]) == 1
    stderr = capsys.readouterr().err
    assert "tool_binding.tool_missing" in stderr
    assert "missing_tool" in stderr
