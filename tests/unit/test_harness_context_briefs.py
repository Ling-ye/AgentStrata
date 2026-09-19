from __future__ import annotations

import hashlib
from pathlib import Path

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.context_briefs import (
    FAILURE_BRIEF_BYTES,
    SOURCE_INDEX_BYTES,
    build_failure_brief,
    build_source_index,
    build_target_context,
)


def repository(tmp_path: Path):
    root = tmp_path / "repo"
    files = {
        "src/chatcopilot/harness/schedule_runtime.py": "class GovernanceScheduler:\n    WorkingDirectory = 'quoted'\n",
        "tests/unit/test_harness_governance_schedule.py": "def test_schedule():\n    assert 'WorkingDirectory'\n",
        "docs/reference/harness-principles.md": "# Principles\nUse real product verification.\n",
        "specs/harness-code-health/spec.md": "---\nstatus: accepted\n---\nGovernanceScheduler systemd timer.\n",
        "src/chatcopilot/evals/vendor/large.py": "vendor = True\n",
        ".env": "SECRET=value\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    manifest = {name: {"sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(), "executable": False}
                for name in files if name != ".env"}
    rules = [{"path": name, "sha256": manifest[name]["sha256"], "content": files[name]}
             for name in ("docs/reference/harness-principles.md", "specs/harness-code-health/spec.md")]
    return root, manifest, rules


def test_source_index_is_deterministic_ranked_and_bounded(tmp_path):
    root, manifest, rules = repository(tmp_path)
    source = {"kind": "code_health", "feedback": {"repair_hint":
        "`GovernanceScheduler` generated invalid WorkingDirectory in src/chatcopilot/harness/schedule_runtime.py"}}
    first = build_source_index(baseline=root, manifest=manifest, source=source, rules=rules, attempt=1)
    second = build_source_index(baseline=root, manifest=manifest, source=source, rules=rules, attempt=1)
    assert first == second
    assert len(json_text(first).encode()) <= SOURCE_INDEX_BYTES
    assert first["candidates"][0]["path"] == "src/chatcopilot/harness/schedule_runtime.py"
    assert any(row["path"].startswith("tests/") for row in first["candidates"])
    assert all(".env" not in row["path"] for row in first["candidates"])
    assert all("/vendor/" not in "/" + row["path"] for row in first["candidates"])
    assert first["rule_refs"][0]["path"] == "docs/reference/harness-principles.md"


def test_source_index_caps_matches_and_reports_omissions(tmp_path):
    root = tmp_path / "repo"
    manifest = {}
    for number in range(80):
        path = root / f"src/chatcopilot/area_{number}/probe.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NeedleSymbol = 1\n" * 4)
        manifest[path.relative_to(root).as_posix()] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "executable": False}
    source = {"feedback": {"repair_hint": "NeedleSymbol " + " ".join(f"`ExtraSymbol{n}`" for n in range(40))}}
    value = build_source_index(baseline=root, manifest=manifest, source=source, rules=[], attempt=2)
    assert len(value["seeds"]) <= 12 and len(value["candidates"]) <= 24
    assert all(len(row["excerpts"]) <= 2 for row in value["candidates"])
    assert value["omitted_counts"]["seeds"] > 0 and value["omitted_counts"]["candidates"] > 0
    assert value["limits"]["truncated"]
    assert len(json_text(value).encode()) <= SOURCE_INDEX_BYTES


def test_empty_source_has_repository_map_without_inventing_candidates(tmp_path):
    root, manifest, rules = repository(tmp_path)
    value = build_source_index(baseline=root, manifest=manifest, source={"kind": "code_health"}, rules=rules, attempt=1)
    assert value["candidates"] == [] and value["repository_map"]
    assert value["rule_refs"][0]["reason"] == "principles"


def test_failure_brief_drops_raw_report_and_keeps_routing():
    evidence = {"checks": [{"name": f"check-{n}", "exit_code": 1,
        "failed_ids": [f"test_{n}_{i}" for i in range(30)], "diagnostic": "failure " + "x" * 4000}
        for n in range(20)], "repository_report": {"raw": "never-inline" * 10000}}
    attempt = {"number": 2, "changed_files": [f"src/file_{n}.py" for n in range(50)],
               "feedback": {"requirements": [f"requirement-{n}" for n in range(30)]}}
    value = build_failure_brief(attempt=attempt, stage="regressions", code="product_failure",
        message="candidate regression", signature="signature", evidence=evidence,
        evidence_ref={"kind": "verification_error", "path": "artifacts/" + "a" * 64 + ".json",
                      "sha256": "a" * 64, "revision": 2})
    assert value["recommended_role"] == "coding"
    assert len(value["failed_checks"]) <= 8 and len(value["changed_paths"]) <= 16
    assert len(value["diagnostics"]) <= 4 and len(json_text(value).encode()) <= FAILURE_BRIEF_BYTES
    assert "never-inline" not in json_text(value)
    assert value["omitted_counts"]["failed_checks"] > 0


def test_target_context_keeps_only_referenced_rules_and_bounded_excerpts():
    target = {"id": "unit", "summary": "fix unit", "impact": "timer fails",
        "principle_refs": ["docs/reference/harness-principles.md:2"], "affected_paths": ["src/unit.py"],
        "acceptance_criteria": ["unit parses"], "disposition": "automatic",
        "evidence": [{"path": "src/unit.py", "start_line": 1, "end_line": 1, "excerpt": "x" * 5000}]}
    rules = [{"path": "docs/reference/harness-principles.md", "sha256": "b" * 64,
              "content": "# Principles\nUse real verification.\n"},
             {"path": "specs/unrelated/spec.md", "sha256": "c" * 64, "content": "unrelated"}]
    value = build_target_context(target, rules)
    assert [row["path"] for row in value["rules"]] == ["docs/reference/harness-principles.md"]
    assert len(value["target"]["evidence"][0]["excerpt"]) <= 800
    assert len(json_text(value).encode()) <= SOURCE_INDEX_BYTES


def test_special_artifacts_inline_with_their_own_bounds(tmp_path):
    artifacts = ArtifactRepository(tmp_path / "task")
    source = {"version": 1, "candidates": [{"path": "src/demo.py", "excerpt": "x" * 7000}]}
    ordinary = {"rows": ["x" * 7000]}
    source_navigation = artifacts.navigation(artifacts.put("source_index", 1, source))
    ordinary_navigation = artifacts.navigation(artifacts.put("verification_error", 1, ordinary))
    assert source_navigation["inline"] == source
    assert "inline" not in ordinary_navigation
