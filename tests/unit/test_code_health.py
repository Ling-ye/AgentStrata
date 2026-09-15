from __future__ import annotations

import copy
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.core.source_snapshot import source_manifest
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.code_health import run_task
from chatcopilot.harness.code_health_rules import finding, parse_audit
from chatcopilot.harness.code_health_workspace import save_patch
from chatcopilot.harness.health_ledger import SourceLedger
from chatcopilot.harness.health_policy import protected_paths
from chatcopilot.harness.models import HarnessError, RepairOptions
from console.backend.routes.harness import router


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    files = {".gitignore": ".cache/\n.env\n", "src/chatcopilot/core/example.py": "unused = True\nvalue = 1\n",
             "docs/guide.md": "Current guide\n", "tests/test_example.py": "def test_ok():\n    assert True\n"}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


def start(repo, tmp_path, *, options=None, request="health-request", scope="all"):
    controller = HarnessController(repo, root=tmp_path / "state")
    task = controller.start_code_health(scope, options or RepairOptions("test-model", max_attempts=1),
                                        request_id=request, launch=False)
    return controller, task["task_id"]


def target():
    return finding("hygiene", "src/chatcopilot/core/example.py", 1, "Unused assignment", "unused = True",
                   "Remove unused assignment", detector="ruff")


class Checks:
    def __init__(self, passed=True):
        self.passed = passed
        self.profiles = []
        self.logs = []

    def bind(self, ledger, frozen):
        self.ledger = ledger

    def scan(self, root, scope, cancel):
        cancel()
        return {"checks": [], "findings": [target()] if "unused = True" in (root / target()["path"]).read_text() else []}

    def verify(self, root, profile, cancel, *, baseline=None):
        cancel()
        self.profiles.append(profile)
        return {"profile": profile, "passed": self.passed, "checks": []}


class Coder:
    def __init__(self, *, review="approved", mutate_review=False, cancel=None):
        self.decision = review
        self.mutate_review = mutate_review
        self.cancel = cancel
        self.calls = []

    def audit(self, root, evidence, options, output, check_cancel):
        self.calls.append("audit")
        return {"findings": [], "inspected_paths": [target()["path"]], "summary": "Reviewed source",
                "submitted_batch": evidence["batch"]["id"],
                "submitted_blocks": [b["block_sha256"] for b in evidence["batch"]["blocks"]]}

    def run(self, root, evidence, options, output, check_cancel):
        self.calls.append("run")
        if self.cancel:
            self.cancel()
            check_cancel()
        path = root / target()["path"]
        path.write_text(path.read_text().replace("unused = True\n", ""))
        return {"events": [], "usage": {}}

    def review(self, root, evidence, options, output, check_cancel):
        self.calls.append("review")
        if self.mutate_review:
            (root / target()["path"]).write_text("value = 'changed after check'\n")
        return {"decision": self.decision, "problem": "" if self.decision == "approved" else "Missing evidence",
                "reason": "Verified behavior" if self.decision == "approved" else "Needs more evidence",
                "evidence_refs": ["source", "patch", "verification"]}


def test_snapshot_patch_preserves_dirty_index_new_deleted_and_modes(repo, tmp_path):
    path = repo / target()["path"]
    path.write_text("unused = True\nvalue = 7\n")
    git(repo, "add", str(path.relative_to(repo)))
    path.write_text("unused = True\nvalue = 9\n")
    new = repo / "docs/new guide.md"
    new.write_text("user new file\n")
    new.chmod(0o755)
    (repo / "docs/guide.md").unlink()
    (repo / ".env").write_text("PRIVATE_DATA=fixture\n")
    index = (repo / ".git/index").read_bytes()
    before = source_manifest(repo)
    controller, task_id = start(repo, tmp_path)
    raw = controller.store.get(task_id)
    assert raw["baseline_manifest"] == before
    assert ".env" not in before
    outcome = run_task(controller.store, task_id, Coder(), checks=Checks())
    assert outcome["status"] == "fixed"
    assert source_manifest(repo) == before
    assert (repo / ".git/index").read_bytes() == index
    patch = controller.patch(task_id, 1)
    assert b"-unused = True" in patch and b"value = 9" in patch
    assert b"value = 1" not in patch and b"user new file" not in patch
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_bytes(patch)
    git(repo, "apply", "--check", str(patch_path))
    candidate = Path(outcome["worktree"])
    assert not (candidate / "docs/guide.md").exists()
    assert (candidate / "docs/new guide.md").stat().st_mode & 0o111
    assert git(candidate, "rev-parse", "HEAD").decode().strip() == outcome["base_commit"]
    assert controller.get(task_id)["candidate_available"] is True


def test_request_identity_and_history_filter(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    same = controller.start_code_health("all", RepairOptions("test-model", max_attempts=1), request_id="health-request", launch=False)
    assert same["task_id"] == ident
    with pytest.raises(HarnessError, match="请求 ID"):
        controller.start_code_health("docs", RepairOptions("test-model"), request_id="health-request", launch=False)
    assert controller.list(kind="code_health")["total"] == 1
    assert controller.list(kind="repair")["total"] == 0
    assert "baseline_manifest" not in controller.list(kind="code_health")["tasks"][0]
    with pytest.raises(HarnessError, match="重新启动"):
        controller.resume(ident)


def test_clean_repository_audits_without_failure_case(repo, tmp_path):
    (repo / target()["path"]).write_text("value = 1\n")
    controller, ident = start(repo, tmp_path)
    coder = Coder()
    result = run_task(controller.store, ident, coder, checks=Checks())
    assert result["status"] == "not_reproduced"
    assert coder.calls == ["audit", "audit"]
    assert "case_id" not in result["source"]


@pytest.mark.parametrize("decision", ["rejected", "inconclusive"])
def test_review_is_independent_from_publication(repo, tmp_path, decision):
    controller, ident = start(repo, tmp_path)
    result = run_task(controller.store, ident, Coder(review=decision), checks=Checks())
    assert result["status"] == "failed"
    assert not result["review_and_commit"] and "local_commit" not in result
    assert "unused = True" in (Path(result["worktree"]) / target()["path"]).read_text()
    assert controller.store.attempts(ident)[0]["review"]["decision"] == decision


def test_failed_checks_are_never_accepted(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    coder = Coder()
    result = run_task(controller.store, ident, coder, checks=Checks(passed=False))
    assert result["status"] == "failed"
    assert coder.calls == ["audit", "run", "audit"]
    assert controller.store.attempts(ident)[0]["verification"]["passed"] is False


def test_cancellation_and_review_drift_do_not_claim_success(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    result = run_task(controller.store, ident, Coder(cancel=lambda: controller.store.update(ident, status="cancel_requested")), checks=Checks())
    assert result["status"] == "cancelled"
    other, ident = start(repo, tmp_path / "other")
    result = run_task(other.store, ident, Coder(mutate_review=True), checks=Checks())
    assert result["status"] == "blocked" and result["error_code"] == "workspace_changed"
    assert not other.get(ident)["candidate_available"]


def test_protected_changes_fail_and_document_changes_are_allowed(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    baseline = controller.store.get(ident)["baseline_manifest"]
    ledger = SourceLedger(controller.store.root / "jobs" / ident / "source", baseline, tmp_path / "inventory", repo)
    (repo / "docs/guide.md").write_text("Updated guide\n")
    assert ledger.changes(repo, baseline, "docs")[0] == ["docs/guide.md"]
    (repo / "tests/test_example.py").write_text("def test_ok():\n    pass\n")
    with pytest.raises(HarnessError, match="固定标准"):
        ledger.changes(repo, baseline, "all")


def test_source_drift_during_freeze_is_blocked(repo, tmp_path, monkeypatch):
    from chatcopilot.harness import api
    original = api.copy_sources

    def changing(source, destination, manifest):
        original(source, destination, manifest)
        (source / target()["path"]).write_text("concurrent change\n")

    monkeypatch.setattr(api, "copy_sources", changing)
    controller, ident = start(repo, tmp_path)
    result = controller.get(ident)
    assert result["status"] == "blocked" and result["error_code"] == "source_changed"


def test_audit_requires_existing_in_scope_evidence(repo):
    item = {k: v for k, v in target().items() if k not in {"id", "detector"}}
    value = {"findings": [item], "inspected_paths": [target()["path"]], "summary": "Checked"}
    assert len(parse_audit(value, repo, "runtime")["findings"]) == 1
    for path in ("../outside", "/tmp/outside", "missing.py", "docs/guide.md"):
        invalid = copy.deepcopy(value)
        invalid["findings"][0]["path"] = path
        with pytest.raises(HarnessError):
            parse_audit(invalid, repo, "runtime")


def test_code_health_route_reuses_host_and_rejects_authority_fields():
    app = FastAPI()
    app.include_router(router)
    app.state.harness = SimpleNamespace(start_code_health=Mock(return_value={"task_id": "repair-example"}))
    body = {"model": "test-model", "request_id": "request-health"}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.post("/api/harness/code-health/tasks", json=body).status_code == 200
        for extra in ({"review_and_commit": True}, {"repository": "/tmp"}, {"command": "anything"}):
            assert client.post("/api/harness/code-health/tasks", json={**body, **extra}).status_code == 422
        assert client.post("/api/harness/code-health/tasks", json=body, headers={"Origin": "https://example.org"}).status_code == 403
    with TestClient(app, client=("192.0.2.5", 5000)) as client:
        assert client.post("/api/harness/code-health/tasks", json=body).status_code == 403
    call = app.state.harness.start_code_health.call_args
    assert call.args[0] == "all" and call.kwargs == {"request_id": "request-health"}


def test_patch_digest_is_rechecked(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    run_task(controller.store, ident, Coder(), checks=Checks())
    path = controller.store.root / "jobs" / ident / "attempt-1/candidate.patch"
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b"tampered patch")
    assert original != hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(HarnessError, match="验证记录"):
        controller.patch(ident, 1)


def test_audit_without_reading_source_is_incomplete(repo):
    with pytest.raises(HarnessError, match="没有读取"):
        parse_audit({"findings": [], "inspected_paths": [], "summary": "Could not start file tools"}, repo, "runtime")


def test_native_and_outer_permissions_agree_for_protected_directory(repo):
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    from chatcopilot.agent.backends.codex_permissions import permission_config
    from chatcopilot.contracts.execution_scope import ExecutionScope
    protected = repo / "tests"
    assert protected in protected_paths(repo)
    scope = ExecutionScope(readable_roots=(repo,), writable_roots=(repo / "src",),
                           protected_roots=(protected,), native_write=True)
    config = permission_config(scope, workdir=repo, private_paths=(str(protected / "secret"),), network_access=False)
    fs = tomllib.loads(next(value for value in config if value.startswith("permissions.agentstrata.filesystem=")))["permissions"]["agentstrata"]["filesystem"]
    assert fs[str(protected)] == "read"
    assert fs[str(protected / "secret")] == "deny"


def test_disposable_index_contains_working_bytes_without_staging(repo, tmp_path):
    from chatcopilot.core.source_snapshot import verification_index
    index_before = (repo / ".git/index").read_bytes()
    source = repo / target()["path"]
    source.write_text("value = 'working tree'\n")
    new = repo / "docs/new.md"
    new.write_text("new source\n")
    (repo / "docs/guide.md").unlink()
    environment = {**os.environ, **verification_index(repo, tmp_path / "verification-index")}
    output = subprocess.check_output(["git", "-C", str(repo), "ls-files"], env=environment, text=True)
    assert "docs/new.md" in output and "docs/guide.md" not in output
    blob = subprocess.check_output(["git", "-C", str(repo), "show", ":" + target()["path"]], env=environment)
    assert blob == source.read_bytes()
    assert (repo / ".git/index").read_bytes() == index_before
    assert b"docs/new.md" not in git(repo, "ls-files")


def test_snapshot_patch_supports_binary_unicode_and_deletion(repo, tmp_path):
    from chatcopilot.harness.workspace import prepare
    controller, ident = start(repo, tmp_path)
    task = controller.store.get(ident)
    root = prepare(repo, controller.store.root, ident, task["base_commit"])
    frozen = controller.store.root / "jobs" / ident / "source"
    ledger = SourceLedger(frozen, task["baseline_manifest"], tmp_path / "inventory", repo)
    ledger.install(root, frozen, task["baseline_manifest"])
    (root / "docs/guide.md").unlink()
    (root / "docs/中文 文件.bin").write_bytes(b"\x00\xffbinary")
    names, _ = ledger.changes(root, task["baseline_manifest"], "docs")
    output = tmp_path / "binary.patch"
    save_patch(root, frozen, names, output)
    git(repo, "apply", "--check", str(output))


def test_audit_can_read_contracts_outside_the_selected_edit_scope(repo):
    item = {k: v for k, v in target().items() if k not in {"id", "detector"}}
    result = parse_audit({"findings": [item], "inspected_paths": ["docs/guide.md", target()["path"]],
                          "summary": "Read the contract and its caller"}, repo, "runtime")
    assert result["inspected_paths"] == ["docs/guide.md", target()["path"]]


def test_verification_keeps_known_debt_but_rejects_new_or_unknown_failures():
    from chatcopilot.harness.code_health_checks import compare_verification
    before = {"passed": False, "checks": [
        {"name": "Ruff", "exit_code": 1, "failed_ids": []},
        {"name": "core tests", "exit_code": 1, "failed_ids": ["test_old", "test_fixed"]},
    ]}
    candidate = {"passed": False, "checks": [
        {"name": "Ruff", "exit_code": 1, "failed_ids": []},
        {"name": "core tests", "exit_code": 1, "failed_ids": ["test_old"]},
    ]}
    assert compare_verification(before, candidate) == ["Ruff", "test_old"]
    assert candidate["checks"][0]["existing_failure"] is True
    candidate["checks"][1]["failed_ids"].append("test_new")
    assert compare_verification(before, candidate) is None
    candidate["checks"][1].update(exit_code=2, failed_ids=[])
    assert compare_verification(before, candidate) is None
    assert compare_verification(before, {"passed": False, "checks": []}) is None
    unexplained = {"passed": False, "checks": [{"name": row["name"], "exit_code": 0} for row in before["checks"]]}
    assert compare_verification(before, unexplained) is None


@pytest.mark.parametrize("keep_going,count", [(False, 1), (True, 2)])
def test_repository_gate_keeps_failure_when_collecting_baseline(tmp_path, monkeypatch, keep_going, count):
    import importlib.util
    import json
    import sys
    script = Path(__file__).resolve().parents[2] / "scripts/check_repo.py"
    spec = importlib.util.spec_from_file_location("code_health_repo_gate_test", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_profiles", lambda: {"fast": (
        module.Check("failure", (sys.executable, "-c", "raise SystemExit(1)")),
        module.Check("next check", (sys.executable, "-c", "print('checked')")),
    )})
    output = tmp_path / "report"
    monkeypatch.setattr(sys, "argv", [str(script), "fast", "--report-dir", str(output),
                                     *(["--keep-going"] if keep_going else [])])
    assert module.main() == 1
    report = json.loads((output / "manifest.json").read_text())
    assert report["ok"] is False and report["finished_at"] is not None
    assert len(report["checks"]) == count
    if keep_going:
        assert report["checks"][-1]["status"] == "passed"


class MultiChecks(Checks):
    def scan(self, root, scope, cancel):
        cancel()
        rows = []
        for path in sorted((root / "src").rglob("*.py")):
            if "unused = True" in path.read_text():
                rows.append(finding("hygiene", path.relative_to(root).as_posix(), 1, "Unused assignment", "unused = True",
                                    "Remove", detector="ruff", group_key=path.stem))
        return {"checks": [], "findings": rows}


class MultiCoder(Coder):
    def __init__(self, fail="", cancel=None, decision=False):
        super().__init__()
        self.fail, self.cancel_after, self.decision = fail, cancel, decision

    def audit(self, root, evidence, options, output, check_cancel):
        result = super().audit(root, evidence, options, output, check_cancel)
        if self.decision and evidence["batch"]["area"].startswith("src"):
            result["findings"] = [finding("boundary", target()["path"], 1, "Policy decision", "Business policy unknown",
                "Owner decides", detector="codex", disposition="needs_decision", group_key="decision")]
        return result

    def run(self, root, evidence, options, output, check_cancel):
        self.calls.append("run")
        row = evidence["selected_findings"][0]
        if row["group_key"] == self.fail:
            if self.cancel_after:
                self.cancel_after()
                check_cancel()
            return {}
        path = root / row["path"]
        path.write_text(path.read_text().replace("unused = True\n", ""))
        return {}

    def review(self, root, evidence, options, output, check_cancel):
        return {"decision": "approved", "problem": "", "reason": "Verified", "evidence_refs": ["patch", "verification"]}


def multi_repo(repo):
    for name in ("a", "b", "c"):
        (repo / f"src/chatcopilot/core/{name}.py").write_text("unused = True\nvalue = 1\n")
    (repo / target()["path"]).write_text("value = 1\n")


def test_two_checkpoints_survive_failed_group_and_cumulative_patch(repo, tmp_path):
    multi_repo(repo)
    controller, ident = start(repo, tmp_path, options=RepairOptions("test", max_attempts=3))
    result = run_task(controller.store, ident, MultiCoder(fail="c", decision=True), checks=MultiChecks())
    assert result["status"] == "fixed"
    assert result["governance_summary"] == {"found": 4, "fixed": 2, "needs_decision": 1, "remaining": 1,
        "coverage": "complete", "completed_batches": 2, "total_batches": 2}
    assert len(result["governance"]["checkpoints"]) == 2
    attempts = controller.store.attempts(ident)
    assert [a["number"] for a in attempts] == [1, 2, 3, 4]
    assert [a["group_attempt"] for a in attempts] == [1, 1, 1, 2]
    patch = controller.candidate_patch(ident)
    assert b"a.py" in patch and b"b.py" in patch and b"c.py" not in patch
    assert "unused = True" in (Path(result["worktree"]) / "src/chatcopilot/core/c.py").read_text()
    assert controller.get(ident)["checkpoint_available"] is True
    checkpoint = controller.store.root / "jobs" / ident / result["checkpoint"]["path"] / "candidate.patch"
    checkpoint.write_bytes(b"changed")
    with pytest.raises(HarnessError, match="摘要"):
        controller.candidate_patch(ident)


def test_cancel_keeps_only_accepted_checkpoints_and_incomplete_coverage(repo, tmp_path):
    multi_repo(repo)
    controller, ident = start(repo, tmp_path)
    coder = MultiCoder(fail="c", cancel=lambda: controller.store.update(ident, status="cancel_requested"))
    result = run_task(controller.store, ident, coder, checks=MultiChecks())
    assert result["status"] == "cancelled"
    assert result["governance_summary"]["coverage"] == "partial"
    assert result["governance_summary"]["fixed"] == 2
    assert controller.get(ident)["candidate_available"]
    assert b"c.py" not in controller.candidate_patch(ident)


def test_missing_audit_receipt_never_counts_as_completed(repo, tmp_path):
    (repo / target()["path"]).write_text("value = 1\n")
    controller, ident = start(repo, tmp_path)
    coder = Coder()
    coder.audit = lambda *args: {"findings": [], "summary": "Looks good", "inspected_paths": []}
    result = run_task(controller.store, ident, coder, checks=Checks())
    assert result["status"] == "blocked"
    assert result["governance_summary"]["coverage"] == "partial"
    assert all(b["error_code"] == "audit_incomplete" for b in result["governance"]["coverage"])


def test_legacy_counts_are_projected_without_writing_history(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    source = controller.store.get(ident)["source"]
    source.pop("governance_version")
    row = {**target(), "disposition": "needs_decision"}
    controller.store.update(ident, source=source, status="not_reproduced", governance={"findings": [row]})
    original = controller.store.get(ident)
    detail = controller.get(ident)
    history = controller.list(kind="code_health")["tasks"][0]
    assert detail["governance_summary"] == history["governance_summary"]
    assert detail["governance_summary"]["found"] == detail["governance_summary"]["needs_decision"] == 1
    assert detail["governance_summary"]["fixed"] == 0
    assert detail["governance_summary"]["coverage"] == "unknown"
    assert controller.store.get(ident) == original


class SemanticChecks(Checks):
    def scan(self, root, scope, cancel):
        cancel()
        return {"checks": [], "findings": []}

    def run_test(self, root, content, cancel):
        cancel()
        failed = "unused = True" in (root / target()["path"]).read_text()
        return {"exit_code": int(failed), "collected": ["test_behavior"], "errors": [],
                "rows": {"test_behavior": {"outcome": "failed" if failed else "passed", "assertion_failure": failed}}}


class SemanticCoder(Coder):
    def audit(self, root, evidence, options, output, check_cancel):
        result = super().audit(root, evidence, options, output, check_cancel)
        if evidence["batch"]["area"].startswith("src"):
            result["findings"] = [{**target(), "detector": "codex", "rule_id": "obsolete"}]
        return result

    def prepare(self, root, evidence, options, output, check_cancel):
        import json
        self.calls.append("prepare")
        draft = output / "draft"
        draft.mkdir(mode=0o700)
        for name, text in {"test_reproduction.py": "def test_behavior():\n    assert True\n",
            "diagnosis.json": json.dumps({"kind": "bugfix", "reason": "Known behavior contract"})}.items():
            (draft / name).write_text(text)
            (draft / name).chmod(0o600)
        return {}


def test_semantic_group_prepares_reviews_freezes_and_delivers_regression(repo, tmp_path):
    controller, ident = start(repo, tmp_path)
    coder = SemanticCoder()
    result = run_task(controller.store, ident, coder, checks=SemanticChecks())
    assert result["status"] == "fixed"
    assert coder.calls == ["audit", "prepare", "review", "run", "review", "audit"]
    group = result["governance"]["groups"][0]
    assert group["proof"]["baseline"]["exit_code"] == 1
    attempt = controller.store.attempts(ident)[0]
    assert attempt["regression_test"]["exit_code"] == 0
    tests = [n for n in attempt["changed_files"] if n.startswith("tests/")]
    assert len(tests) == 1 and (Path(result["worktree"]) / tests[0]).is_file()
    assert b"test_behavior" in controller.candidate_patch(ident)
    assert not (repo / tests[0]).exists()


def test_failed_dependency_is_deferred_while_independent_group_runs(repo, tmp_path):
    multi_repo(repo)
    class DependencyChecks(MultiChecks):
        def scan(self, root, scope, cancel):
            result = super().scan(root, scope, cancel)
            for row in result["findings"]:
                if row["group_key"] == "b":
                    row["depends_on"] = ["a"]
            return result
    controller, ident = start(repo, tmp_path)
    result = run_task(controller.store, ident, MultiCoder(fail="a"), checks=DependencyChecks())
    groups = {g["key"]: g["status"] for g in result["governance"]["groups"]}
    assert groups == {"a": "failed", "b": "deferred", "c": "accepted"}


def test_budget_preserves_checkpoint_and_marks_scope_incomplete(repo, tmp_path, monkeypatch):
    from chatcopilot.harness import code_health
    multi_repo(repo)
    now = [10.0]
    monkeypatch.setattr(code_health.time, "monotonic", lambda: now[0])
    class BudgetCoder(MultiCoder):
        def run(self, root, evidence, options, output, check_cancel):
            if evidence["selected_findings"][0]["group_key"] == "b":
                now[0] = 200.0
                check_cancel()
            return super().run(root, evidence, options, output, check_cancel)
    controller, ident = start(repo, tmp_path, options=RepairOptions("test", timeout_seconds=100))
    result = run_task(controller.store, ident, BudgetCoder(), checks=MultiChecks())
    assert result["error_code"] == "budget_exhausted"
    assert result["governance_summary"]["fixed"] == 1
    assert result["governance_summary"]["coverage"] == "partial"
    assert controller.get(ident)["checkpoint_available"]


def test_semantic_bug_cannot_downgrade_its_proof_to_refactor(repo, tmp_path):
    import json
    class DowngradeCoder(SemanticCoder):
        def prepare(self, root, evidence, options, output, check_cancel):
            super().prepare(root, evidence, options, output, check_cancel)
            (output / "draft/diagnosis.json").write_text(json.dumps({"kind": "refactor", "reason": "No failure needed", "structural_before": "Duplicate code"}))
    controller, ident = start(repo, tmp_path)
    coder = DowngradeCoder()
    result = run_task(controller.store, ident, coder, checks=SemanticChecks())
    assert result["governance_summary"]["fixed"] == 0
    assert result["governance_summary"]["needs_decision"] == 1
    assert "run" not in coder.calls
    assert "不能把行为缺陷" in result["governance"]["groups"][0]["reason"]


def test_old_acceptance_is_not_recomputed_with_new_inventory_rules(repo, tmp_path, monkeypatch):
    controller, ident = start(repo, tmp_path)
    source = controller.store.get(ident)['source']
    source.pop('governance_version')
    controller.store.update(ident, status='fixed', source=source, governance={'findings': [target()], 'resolved_ids': [target()['id']]})
    monkeypatch.setattr(controller, '_candidate_available', Mock(side_effect=AssertionError('legacy result was rejudged')))
    original = controller.store.get(ident)
    result = controller.get(ident)
    assert result['candidate_available'] is None
    assert result['status'] == 'fixed' and result['governance_summary']['fixed'] == 1
    assert controller.store.get(ident) == original


def test_passing_exit_cannot_hide_changed_tests_or_new_skips():
    from chatcopilot.harness.code_health_checks import compare_verification
    baseline = {'passed': True, 'checks': [{'name': 'core tests', 'exit_code': 0,
        'test_inventory': {'sha256': 'fixed-tests', 'count': 2, 'skipped_ids': []}}]}
    candidate = copy.deepcopy(baseline)
    assert compare_verification(baseline, candidate) == []
    candidate['checks'][0]['test_inventory']['skipped_ids'] = ['test_missing']
    assert compare_verification(baseline, candidate) is None
    candidate['checks'][0]['test_inventory'] = {'sha256': 'fewer-tests', 'count': 1, 'skipped_ids': []}
    assert compare_verification(baseline, candidate) is None
    candidate['checks'][0].pop('test_inventory')
    assert compare_verification(baseline, candidate) is None


def test_preparation_can_correct_its_reason_without_rewriting_a_valid_test(repo, tmp_path):
    import json
    class CorrectingCoder(SemanticCoder):
        def __init__(self):
            super().__init__()
            self.revisions = 0
        def prepare(self, root, evidence, options, output, check_cancel):
            self.revisions += 1
            super().prepare(root, evidence, options, output, check_cancel)
            if self.revisions == 1:
                (output / 'draft/diagnosis.json').write_text(json.dumps({'kind': 'bugfix', 'reason': ''}))
    controller, ident = start(repo, tmp_path, options=RepairOptions('test', max_attempts=3))
    coder = CorrectingCoder()
    result = run_task(controller.store, ident, coder, checks=SemanticChecks())
    assert result['status'] == 'fixed' and coder.revisions == 2


def test_out_of_scope_debt_does_not_become_a_decision_group(repo, tmp_path):
    controller, ident = start(repo, tmp_path, scope='docs')
    coder = Coder()
    result = run_task(controller.store, ident, coder, checks=Checks())
    assert result['governance_summary']['found'] == 0
    assert result['governance']['before']['findings']  # Retained for regression comparison.
    assert 'run' not in coder.calls


def test_structural_evidence_must_be_renderable_text(repo, tmp_path):
    import json
    class InvalidEvidenceCoder(SemanticCoder):
        def prepare(self, root, evidence, options, output, check_cancel):
            super().prepare(root, evidence, options, output, check_cancel)
            (output / 'draft/diagnosis.json').write_text(json.dumps({'kind': 'bugfix', 'reason': 'Contract', 'structural_before': {'unexpected': 'object'}}))
    controller, ident = start(repo, tmp_path)
    result = run_task(controller.store, ident, InvalidEvidenceCoder(), checks=SemanticChecks())
    assert result['governance_summary']['needs_decision'] == 1
    assert '结构依据必须为文本' in result['governance']['groups'][0]['reason']


@pytest.mark.parametrize("detached", [False, True])
def test_repository_facts_are_frozen_and_checkpoint_context_is_separate(repo, tmp_path, detached):
    from chatcopilot.harness.code_health import HealthRun
    from chatcopilot.core.source_snapshot import manifest_digest
    if detached:
        git(repo, "checkout", "--detach", "-q")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    (repo / target()["path"]).write_text("unused = True\nvalue = 9\n")
    controller, ident = start(repo, tmp_path)
    task = controller.store.get(ident)
    git(repo, "checkout", "-q", "-b", "after-task-start")
    run = HealthRun(controller.store, ident, Coder(), Checks())
    audit = run.model_source(snapshot=True)["repository_context"]
    assert audit["original_branch"] == (None if detached else branch)
    assert audit["base_commit"] == task["base_commit"]
    assert audit["snapshot_digest"] == manifest_digest(task["baseline_manifest"])
    assert audit["directory_kind"] == "source_snapshot" and audit["git_worktree"] is None
    run.root = tmp_path / "candidate"
    run.checkpoint_manifest = {}
    candidate = run.model_source()["repository_context"]
    assert candidate["snapshot_digest"] == audit["snapshot_digest"]
    assert candidate["checkpoint_digest"] != candidate["snapshot_digest"]
    assert candidate["directory_kind"] == "git_worktree" and candidate["git_worktree"] == str(run.root)
    run.task["source"].pop("original_branch")
    assert run.model_source()["repository_context"]["original_branch"] is None


@pytest.mark.parametrize("stage", ["audit", "prepare", "run", "review"])
def test_environment_error_stops_without_retry_or_product_finding(repo, tmp_path, stage):
    controller, ident = start(repo, tmp_path, options=RepairOptions("test", max_attempts=3))
    coder = SemanticCoder()
    failure = Mock(side_effect=HarnessError("coding_environment", "Git environment unavailable"))
    setattr(coder, stage, failure)
    result = run_task(controller.store, ident, coder, checks=SemanticChecks())
    assert result["status"] == "blocked" and result["error_code"] == "coding_environment"
    assert failure.call_count == 1
    assert result["governance_summary"]["needs_decision"] == 0
    assert result["governance_summary"]["coverage"] == "partial"


def test_environment_error_preserves_earlier_checkpoints(repo, tmp_path):
    multi_repo(repo)
    controller, ident = start(repo, tmp_path, options=RepairOptions("test", max_attempts=3))

    class InterruptedCoder(MultiCoder):
        def run(self, root, evidence, *args):
            if evidence["selected_findings"][0]["group_key"] == "c":
                raise HarnessError("coding_environment", "Git environment unavailable")
            return super().run(root, evidence, *args)

    result = run_task(controller.store, ident, InterruptedCoder(), checks=MultiChecks())
    assert result["status"] == "blocked" and result["error_code"] == "coding_environment"
    assert result["governance_summary"]["fixed"] == 2
    assert len(controller.store.attempts(ident)) == 3
    patch = controller.candidate_patch(ident)
    assert b"a.py" in patch and b"b.py" in patch and b"c.py" not in patch
    assert controller.get(ident)["candidate_available"] is True


@pytest.mark.parametrize("phase,accepted", [("review", 2), ("checkpoint", 2), ("after_commit", 3)])
@pytest.mark.parametrize("code,name", [(5898, "SQLITE_IOERR_DELETE_NOENT"), (13, "SQLITE_FULL"), (8, "SQLITE_READONLY")])
def test_storage_error_stops_groups_and_keeps_only_durable_checkpoints(repo, tmp_path, monkeypatch, phase, accepted, code, name):
    multi_repo(repo)
    controller, ident = start(repo, tmp_path, options=RepairOptions("test", max_attempts=3))
    store = controller.store
    error = sqlite3.OperationalError("injected storage failure")
    error.sqlite_errorcode, error.sqlite_errorname = code, name
    original_connect, original_update = sqlite3.connect, store.update
    writes, reviews = [], []

    class Connection(sqlite3.Connection):
        def commit(self):
            if phase == "checkpoint":
                raise error
            super().commit()
        def close(self):
            super().close()
            if phase == "after_commit":
                raise error

    def update(task_id, **changes):
        if changes.get("accepted_attempt", {}).get("number") == 3 and phase != "review":
            writes.append(True)
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, **kw, factory=Connection))
                return original_update(task_id, **changes)
        return original_update(task_id, **changes)

    class StorageCoder(MultiCoder):
        def review(self, root, evidence, *args):
            key = evidence["source"]["selected_findings"][0]["group_key"]
            reviews.append(key)
            if key == "c" and phase == "review":
                with store.database.connect(write=True):
                    raise error
            return super().review(root, evidence, *args)

    monkeypatch.setattr(store, "update", update)
    result = run_task(store, ident, StorageCoder(), checks=MultiChecks())
    assert result["status"] == "blocked" and result["error_code"] == "storage_error"
    assert result["stage"] == "done" and result["current_source"] is None
    assert result["storage_error"]["sqlite_errorcode"] == code
    assert result["storage_error"]["sqlite_errorname"] == name
    assert reviews == ["a", "b", "c"] and len(writes) == int(phase != "review")
    attempts = store.attempts(ident)
    assert [a["status"] for a in attempts] == ["accepted"] * accepted + ["interrupted"] * (3 - accepted)
    if accepted < 3:
        assert attempts[-1]["counts_toward_budget"] is False
        assert attempts[-1]["storage_error"] == result["storage_error"]
    assert len(result["governance"]["checkpoints"]) == accepted
    assert result["governance_summary"]["fixed"] == accepted
    patch = controller.candidate_patch(ident)
    assert b"a.py" in patch and b"b.py" in patch
    assert (b"c.py" in patch) == (accepted == 3)
    assert controller.get(ident)["candidate_available"] is True


def test_persistent_storage_failure_preserves_original_exception(repo, tmp_path, monkeypatch, caplog):
    controller, ident = start(repo, tmp_path)
    original = sqlite3.OperationalError("first I/O failure")
    secondary = sqlite3.OperationalError("terminal write unavailable")
    coder = Coder()
    def review(*args):
        monkeypatch.setattr(sqlite3, "connect", Mock(side_effect=secondary))
        raise original
    coder.review = review
    with pytest.raises(sqlite3.OperationalError) as caught:
        run_task(controller.store, ident, coder, checks=Checks())
    assert caught.value is original
    assert "terminal state could not be persisted" in caplog.text
    # The worker exits; once storage is available the normal worker-loss path
    # terminalizes its unfinished attempts. It never reruns the business body.
    monkeypatch.undo()
    result = controller.store.interrupt(ident)
    assert result["status"] == "interrupted"
    assert [a["status"] for a in controller.store.attempts(ident)] == ["interrupted"]
    assert controller.get(ident)["candidate_available"] is False
