from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from chatcopilot.core.source_snapshot import source_manifest, copy_sources
from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.harness.health_batches import build_batches, descriptor, summary
from chatcopilot.harness.health_ledger import SourceLedger
from chatcopilot.harness.health_policy import protected_paths, writable_paths, policy_path
from chatcopilot.harness.code_health_checks import CodeHealthChecks
from chatcopilot.harness.models import HarnessError


def fixture(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in {".gitignore": "ignored/\n.env\n", ".env.example": "PUBLIC=example\n",
        "src/demo.py": "value = 1\n", "scripts/check_repo.py": "raise SystemExit(1)\n",
        "tests/test_demo.py": "def test_true():\n    assert True\n", "pyproject.toml": ""}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = source_manifest(root)
    frozen = tmp_path / "frozen"
    copy_sources(root, frozen, manifest)
    ledger = SourceLedger(frozen, manifest, tmp_path / "inventory", root)
    return root, frozen, ledger


@pytest.mark.parametrize("name", [".env", ".env.local", "local.env", "bots/demo/local.env", ".env/private.example", ".env/env.example"])
def test_private_environment_is_excluded(name):
    assert not is_deployable_source_path(name)


@pytest.mark.parametrize("name", [".env.example", "env.example", "deploy/wsl/console.env.example", "bots/demo/local.env.example"])
def test_public_environment_template_is_included(name):
    assert is_deployable_source_path(name)


def test_fixed_inventory_cannot_be_hidden_by_candidate_ignores_or_implementation(tmp_path):
    root, frozen, ledger = fixture(tmp_path)
    assert ".env.example" in ledger.original
    (root / "src/demo.py").write_text("value = 2\n")
    (root / ".gitignore").write_text("src/\n.env.example\n")
    assert ledger.manifest(root)["src/demo.py"]["sha256"] != ledger.original["src/demo.py"]["sha256"]
    with pytest.raises(HarnessError, match="固定标准"):
        ledger.changes(root, ledger.original, "all")
    ledger.install(root, frozen, ledger.original)
    (root / "src/.gitignore").write_text("*\n")
    with pytest.raises(HarnessError, match="忽略规则"):
        ledger.manifest(root)


def test_checker_candidate_is_tested_but_never_used_as_judge(tmp_path):
    root, frozen, ledger = fixture(tmp_path)
    (root / "scripts/check_repo.py").write_text("raise SystemExit(0)\n")
    names, _ = ledger.changes(root, ledger.original, "all")
    assert names == ["scripts/check_repo.py"]
    checks = CodeHealthChecks(tmp_path / "checks", root)
    checks.bind(ledger, frozen)
    checks.view(root, tmp_path / "grade")
    checks.view(root, tmp_path / "candidate", checkers=False)
    assert (tmp_path / "grade/scripts/check_repo.py").read_text() == "raise SystemExit(1)\n"
    assert (tmp_path / "candidate/scripts/check_repo.py").read_text() == "raise SystemExit(0)\n"
    code, _ = checks.command(tmp_path / "grade", [sys.executable, "scripts/check_repo.py"],
                             tmp_path / "checks/native", lambda: None, reads=(frozen,))
    assert code == 1
    (root / "tests/test_demo.py").write_text("def test_true(): pass\n")
    with pytest.raises(HarnessError, match="固定标准"):
        ledger.changes(root, ledger.original, "all")


def test_candidate_implementation_writable_but_tests_and_git_read_only(tmp_path):
    root, _, _ = fixture(tmp_path)
    assert root in writable_paths(root, "all")
    protected = protected_paths(root)
    assert root / ".git" in protected and root / "tests" in protected
    assert root / "scripts/check_repo.py" not in protected
    assert root / "tests/test_demo.py" not in protected  # Already covered by its directory.


def test_large_file_blocks_are_complete_and_binary_is_not_claimed_as_read(tmp_path):
    root, _, _ = fixture(tmp_path)
    content = "长文件内容\n" * 15000
    (root / "src/demo.py").write_text(content)
    (root / "src/icon.bin").write_bytes(b"\0\1")
    batches = build_batches(root, source_manifest(root), "all")
    blocks = [b for batch in batches for b in batch["blocks"] if b["path"] == "src/demo.py"]
    assert "".join(b["content"] for b in blocks) == content
    assert all(hashlib.sha256(b["content"].encode()).hexdigest() == b["block_sha256"] for b in blocks)
    coverage = [descriptor(b) for b in batches]
    for item in coverage:
        item["status"] = "unsupported" if any(b.get("binary") for b in item["blocks"]) else "completed"
    assert summary({"coverage": coverage})["coverage"] == "partial"


def test_frozen_pytest_imports_candidate_after_loading_real_runner(tmp_path):
    root, frozen, ledger = fixture(tmp_path)
    # Would terminate pytest before collection if loaded as the framework.
    (root / "src/pytest.py").write_text("raise RuntimeError('candidate pytest shadow')\n")
    checks = CodeHealthChecks(tmp_path / "checks", root)
    checks.bind(ledger, frozen)
    result = checks.run_test(root, b"from demo import value\ndef test_value():\n    assert value == 2\n", lambda: None)
    assert result["exit_code"] == 1
    assert all(r["assertion_failure"] for r in result["rows"].values())
    (root / "src/demo.py").write_text("value = 2\n")
    result = checks.run_test(root, b"from demo import value\ndef test_value():\n    assert value == 2\n", lambda: None)
    assert result["exit_code"] == 0


@pytest.mark.parametrize("name", ["src/chatcopilot/evals/suites/project-business-v1/cases.yaml",
    "src/chatcopilot/evals/business_policy.py", "console/web/src/features/codeHealth/api.test.ts"])
def test_existing_expectations_and_frontend_tests_remain_fixed(name):
    assert policy_path(name)


def test_frozen_scan_preserves_relative_lint_rules_and_still_finds_errors(tmp_path):
    root, _, _ = fixture(tmp_path)
    (root / "console").mkdir()
    (root / "console/__init__.py").write_text("")
    (root / 'scripts/check_architecture.py').write_text('import json\nprint(json.dumps({"violations": {}}))\n')
    (root / 'scripts/check_sdd_specs.py').write_text('print("OK")\n')
    (root / 'scripts/check_docs.py').write_bytes((Path(__file__).resolve().parents[2] / 'scripts/check_docs.py').read_bytes())
    (root / 'pyproject.toml').write_text('[tool.ruff.lint]\nselect = ["E4", "E7", "E9", "F"]\n[tool.ruff.lint.per-file-ignores]\n"src/demo.py" = ["F401"]\n')
    (root / 'src/demo.py').write_text('import math\n')
    frozen = tmp_path / 'scan-frozen'
    manifest = source_manifest(root)
    copy_sources(root, frozen, manifest)
    ledger = SourceLedger(frozen, manifest, tmp_path / 'scan-inventory', root)
    checks = CodeHealthChecks(tmp_path / 'checks', root)
    checks.bind(ledger, frozen)
    report = checks.scan(root, 'all', lambda: None)
    assert report['findings'] == [], str(report['findings'])
    (root / 'src/demo.py').write_text('import math\nvalue = missing_name\n')
    report = checks.scan(root, 'all', lambda: None)
    assert len(report['findings']) == 1
    assert report['findings'][0]['path'] == 'src/demo.py'
    assert 'F821' in report['findings'][0]['summary']


def test_frozen_document_checker_reads_candidate_and_reports_protected_pages(tmp_path):
    root, _, _ = fixture(tmp_path)
    (root / "scripts/check_sdd_specs.py").write_text('print("OK")\n')
    (root / "scripts/check_docs.py").write_bytes((Path(__file__).resolve().parents[2] / "scripts/check_docs.py").read_bytes())
    (root / "docs/reference").mkdir(parents=True)
    (root / "README.md").write_text("# Fixture\n\n[rule](docs/reference/domain.md)\n")
    (root / "docs/reference/domain.md").write_text("# Domain\n\n## 源码入口\n[code](../../src/demo.py)\n[bad](#missing)\n")
    manifest = source_manifest(root)
    frozen = tmp_path / "docs-frozen"
    copy_sources(root, frozen, manifest)
    ledger = SourceLedger(frozen, manifest, tmp_path / "docs-inventory", root)
    # A candidate implementation cannot substitute its always-successful checker.
    (root / "scripts/check_docs.py").write_text('print(\'{"violations": [], "review_hints": []}\')\n')
    checks = CodeHealthChecks(tmp_path / "checks", root)
    checks.bind(ledger, frozen)
    report = checks.scan(root, "docs", lambda: None)
    assert len(report["findings"]) == 1
    row = report["findings"][0]
    assert row["path"] == "docs/reference/domain.md" and row["disposition"] == "needs_decision"
    assert "anchor" in row["evidence"]
    batches = build_batches(frozen, manifest, "docs")
    assert any(b["path"] == row["path"] for batch in batches for b in batch["blocks"])
    (root / "docs/reference/domain.md").rename(root / "docs/ordinary.md")
    with pytest.raises(HarnessError, match="固定标准"):
        ledger.changes(root, manifest, "docs")


def test_root_document_write_is_file_scoped_in_real_sandbox(tmp_path):
    from chatcopilot.contracts.execution_scope import ExecutionScope
    from chatcopilot.core.scoped_process import sandbox_command

    root = tmp_path / "candidate"
    root.mkdir()
    readme = root / "README.md"
    protected = root / "AGENTS.md"
    sibling = root / "ordinary.txt"
    security, conduct = root / "SECURITY.md", root / "CODE_OF_CONDUCT.md"
    for path in (readme, protected, sibling, security, conduct):
        path.write_text("original")
    scope = ExecutionScope(readable_roots=(root, Path(sys.prefix).resolve()),
                           writable_roots=writable_paths(root, "docs"),
                           protected_roots=protected_paths(root))
    assert scope.writable_roots == (readme,)
    code = """from pathlib import Path
import sys
root = Path(sys.argv[1])
(root / 'README.md').write_text('changed')
for name in ('AGENTS.md', 'SECURITY.md', 'CODE_OF_CONDUCT.md', 'ordinary.txt', 'new.py'):
    try:
        (root / name).write_text('forbidden')
    except OSError:
        continue
    raise RuntimeError('unexpected write authority')
"""
    result = subprocess.run(sandbox_command([sys.executable, "-c", code, str(root)], scope=scope, cwd=root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert readme.read_text() == "changed"
    assert protected.read_text() == sibling.read_text() == "original"
    assert security.read_text() == conduct.read_text() == "original"
    assert not (root / "new.py").exists()
