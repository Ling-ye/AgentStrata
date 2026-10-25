from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def summary(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("ci_summary")


def test_summary_uses_failure_identities_and_skip_reasons_without_raw_payloads(summary, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"ok": False, "finished_at": 2, "checks": [{
        "name": "full Python tests", "status": "failed", "elapsed_seconds": 2,
        "failed_ids": ["tests/test_case.py::test_failure"], "first_failure": "private-failure-payload",
        "log_path": "private-log-location", "test_inventory": {
            "skipped_ids": ["tests.test_optional::test_native"], "error_ids": ["tests.test_setup::test_error"]},
    }]}))
    (tmp_path / "01-pytest.xml").write_text('''<testsuites><testsuite>
      <testcase classname="tests.test_optional" name="test_native"><skipped message="native CLI unavailable"/></testcase>
      <testcase classname="tests.test_case" name="test_failure"><failure>private-junit-payload</failure><system-out>private-system-output</system-out></testcase>
    </testsuite></testsuites>''')
    text = summary.render(tmp_path, "Python")
    assert "failed" in text and "tests/test_case.py::test_failure" in text
    assert "tests.test_setup::test_error" in text and "native CLI unavailable" in text
    assert "Skipped tests (1)" in text
    assert not any(value in text for value in ("private-failure-payload", "private-log-location",
                                               "private-junit-payload", "private-system-output"))


def test_summary_does_not_read_stale_junit_without_current_inventory(summary, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"ok": False, "finished_at": None,
        "checks": [{"name": "full Python tests", "status": "running"}]}))
    (tmp_path / "01-pytest.xml").write_text('<testsuite><testcase classname="stale" name="old"><skipped message="old run"/></testcase></testsuite>')
    text = summary.render(tmp_path, "Current")
    assert "incomplete" in text and "old run" not in text and "stale::old" not in text


@pytest.mark.parametrize("payload", [None, "not JSON", '{"checks": 123}'])
def test_missing_or_invalid_report_is_unavailable_not_passed(summary, tmp_path, payload):
    if payload is not None:
        (tmp_path / "manifest.json").write_text(payload)
    text = summary.render(tmp_path, "Gate")
    assert "未标记为通过" in text and "**passed**" not in text


def test_missing_skip_xml_retains_manifest_skip_ids(summary, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"ok": True, "finished_at": 1,
        "checks": [{"name": "tests", "status": "passed", "test_inventory": {"skipped_ids": ["m::optional"]}}]}))
    text = summary.render(tmp_path, "Gate")
    assert "m::optional" in text and "不可读取" in text and "Skipped tests (1)" in text


def test_summary_appends_to_github_step_summary_and_escapes_markup(summary, tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(json.dumps({"ok": True, "finished_at": 1,
        "checks": [{"name": "test|<name>", "status": "passed", "elapsed_seconds": 1}]}))
    output = tmp_path / "summary.md"
    output.write_text("Existing summary\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(output))
    assert summary.main(["--report-dir", str(tmp_path), "--title", "Python"]) == 0
    text = output.read_text()
    assert text.startswith("Existing summary\n") and "test\\|&lt;name&gt;" in text and "**passed**" in text
