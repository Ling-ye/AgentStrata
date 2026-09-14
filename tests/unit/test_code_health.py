from __future__ import annotations

import copy
import hashlib
import os
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
from chatcopilot.harness import code_health_workspace as workspace
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
        return {"findings": [], "inspected_paths": [target()["path"]], "summary": "Reviewed source"}

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
    assert coder.calls == ["audit"]
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
    assert coder.calls == ["audit", "run"]
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
    (repo / "docs/guide.md").write_text("Updated guide\n")
    assert workspace.changes(repo, baseline, "docs") == ["docs/guide.md"]
    (repo / "tests/test_example.py").write_text("def test_ok():\n    pass\n")
    with pytest.raises(HarnessError, match="受保护"):
        workspace.changes(repo, baseline, "all")


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
    protected = repo / "src/chatcopilot/authorization"
    protected.mkdir()
    assert protected not in workspace.writable_paths(repo, "runtime")
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
    workspace.restore(root, frozen, task["baseline_manifest"])
    (root / "docs/guide.md").unlink()
    (root / "docs/中文 文件.bin").write_bytes(b"\x00\xffbinary")
    names = workspace.changes(root, task["baseline_manifest"], "docs")
    output = tmp_path / "binary.patch"
    workspace.save_patch(root, frozen, names, output)
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
