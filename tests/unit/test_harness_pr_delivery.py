"""Real Git lifecycle tests against a local bare remote; GitHub responses are simulated."""
from __future__ import annotations

import copy
from pathlib import Path
import subprocess

import pytest

from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest
from chatcopilot.harness import delivery, delivery_archive
from chatcopilot.harness.delivery_candidate import candidate, commit
from chatcopilot.harness.code_health_workspace import save_patch
from chatcopilot.harness.github_delivery import DeliveryConfig, GitHubDelivery, git
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION, GOVERNANCE_VERSION
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.delivery_runtime import pending


def command(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout.decode().strip()


class Remote(GitHubDelivery):
    def __init__(self, bare):
        super().__init__(DeliveryConfig("acme/project", "test-actor", "unit-test-token-not-real", "Lingye", "616202172@qq.com"))
        self.bare = bare
        self.pr = None
        self.creations = 0
        self.enable_count = 0
        self.disabled = 0
        self.fail_create_after = False
        self.fail_push_after = False
        self.ready = False

    @property
    def url(self):
        return self.bare.as_uri()

    def request(self, method, path, **kwargs):
        if path == "/user":
            return {"login": "test-actor", "type": "User"}
        if path.endswith("/branches/main"):
            return {"commit": {"sha": command(self.bare, "rev-parse", "main")}, "protected": True}
        if path.endswith("/pulls"):
            if method == "GET":
                return [copy.deepcopy(self.pr)] if self.pr else []
            self.creations += 1
            body = kwargs["body"]
            head = command(self.bare, "rev-parse", body["head"])
            self.pr = {"number": 1, "node_id": "PR_test", "html_url": "https://github.com/acme/project/pull/1",
                       "state": "open", "draft": body["draft"], "merged": False, "auto_merge": None,
                       "head": {"ref": body["head"], "sha": head, "repo": {"full_name": "acme/project"}},
                       "base": {"ref": "main", "repo": {"full_name": "acme/project"}}}
            if self.fail_create_after:
                self.fail_create_after = False
                raise HarnessError("network_uncertain", "create response lost")
            return copy.deepcopy(self.pr)
        if path.endswith("/pulls/1"):
            if self.pr:
                # The remote PR head tracks the real pushed branch.
                try:
                    self.pr["head"]["sha"] = command(self.bare, "rev-parse", self.pr["head"]["ref"])
                except subprocess.CalledProcessError:
                    pass
            return copy.deepcopy(self.pr)
        raise AssertionError((method, path))

    def push(self, *args, **kwargs):
        super().push(*args, **kwargs)
        if self.fail_push_after:
            self.fail_push_after = False
            raise HarnessError("network_uncertain", "push response lost")

    def enable(self, state):
        assert self.pr["head"]["sha"] == state["commit_sha"]
        self.pr["auto_merge"] = {"merge_method": "squash"}
        self.enable_count += 1

    def disable(self, state):
        self.pr["auto_merge"] = None
        self.disabled += 1

    def check_summary(self, state):
        return {"rows": [], "failed": False}

    def merge_ready(self, state):
        return self.ready

    def merge(self, state):
        self.mark_merged(state)

    def mark_merged(self, state):
        tree = command(self.bare, "rev-parse", state["commit_sha"] + "^{tree}")
        parent = command(self.bare, "rev-parse", "main")
        from chatcopilot.core.github_transport import git_environment
        sha = git(self.bare, "commit-tree", tree, "-p", parent, data=b"squashed repair\n",
                  env=git_environment(author_name="Lingye", author_email="616202172@qq.com")).decode().strip()
        git(self.bare, "update-ref", "refs/heads/main", sha, parent)
        self.pr.update(state="closed", merged=True, merge_commit_sha=sha, auto_merge=None)


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "publication_checks", lambda _store, _ident, cancel: cancel())
    repo = tmp_path / "repo"
    repo.mkdir()
    command(repo, "init", "-b", "main")
    command(repo, "config", "user.name", "Lingye")
    command(repo, "config", "user.email", "616202172@qq.com")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("Old explanation.\n")
    command(repo, "add", "docs/guide.md")
    command(repo, "commit", "-m", "baseline")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(repo), str(bare)], check=True, capture_output=True)
    client = Remote(bare)
    base = command(repo, "rev-parse", "HEAD")
    state = {"version": 1, "state": "pending", "repository": "acme/project", "actor": "test-actor", "base_branch": "main",
             "base_sha": base, "author_name": "Lingye", "author_email": "616202172@qq.com", "auto_merge": False}
    store = HarnessStore(tmp_path / "private")
    ident = "repair-" + "a" * 32
    store.create({"task_id": ident, "pipeline_version": PIPELINE_VERSION, "request_key": ident, "request_digest": ident,
        "context_key": ident, "match_key": ident, "active_key": ident, "repository": str(repo), "base_commit": base,
        "source": {"kind": "code_health", "scope": "docs", "governance_version": GOVERNANCE_VERSION}, "delivery": state,
        "options": {"model": "test", "reasoning_effort": "high", "max_attempts": 1, "budget": {"mode": "fixed_groups", "count": 1}}})
    delivery.initialize(store, ident, client)
    root = Path(store.get(ident)["worktree"])
    (root / "docs/guide.md").write_text("Correct explanation.\n")
    manifest = source_manifest(root)
    checkpoint = store.root / "jobs" / ident / "checkpoints/1"
    checkpoint.mkdir(parents=True)
    copy_sources(root, checkpoint / "source", manifest)
    sha = save_patch(root, store.root / "jobs" / ident / "source", ["docs/guide.md"], checkpoint / "candidate.patch")
    store.save_attempt(ident, 1, {"number": 1, "status": "accepted", "candidate_digest": manifest_digest(manifest),
        "changed_files": ["docs/guide.md"], "review": {"decision": "approved"}, "verification": {"profile": "documentation_only", "passed": True}, "regressions": []})
    store.update(ident, status="fixed", working_digest=manifest_digest(manifest), verified_digest=manifest_digest(manifest),
        verified_manifest=manifest, checkpoint={"path": "checkpoints/1", "patch_sha256": sha, "digest": manifest_digest(manifest)},
        governance_summary={"accepted_groups": 1, "remaining": 2, "coverage": "partial"})
    return store, ident, client, repo


def test_publish_cleanup_and_merge_receipt(task):
    store, ident, client, repo = task
    base = command(repo, "rev-parse", "main")
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "waiting_checks", result
    assert client.creations == 1 and client.enable_count == 1
    assert result["cleanup"]["local"] == "cleaned", result.get("cleanup")
    assert not Path(result["worktree"]).exists()
    assert not command(repo, "for-each-ref", "refs/heads/" + result["branch"])
    assert command(repo, "rev-parse", "main") == base
    folder, archive = delivery_archive.verify_archive(store, result)
    assert (folder / "source/docs/guide.md").read_text() == "Correct explanation.\n"
    assert archive["head"] == result["delivery"]["commit_sha"]
    client.mark_merged(result["delivery"])
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "merged"
    assert result["delivery"]["merge_sha"] != result["delivery"]["commit_sha"]
    assert result["cleanup"]["remote"] == "cleaned"
    assert command(repo, "rev-parse", "main") == base


@pytest.mark.parametrize("failure", ["fail_create_after", "fail_push_after"])
def test_uncertain_remote_write_retries_without_duplicate_commit_or_pr(task, failure):
    store, ident, client, _ = task
    setattr(client, failure, True)
    first = delivery.reconcile(store, ident, client=client)
    assert first["delivery"]["state"] == "blocked"
    sha = first["delivery"]["commit_sha"]
    assert first["cleanup"]["local"] == "cleaned"
    result = delivery.reconcile(store, ident, client=client, retry=True)
    assert result["delivery"]["state"] == "waiting_checks", result
    assert result["delivery"]["commit_sha"] == sha
    assert client.creations == 1


def test_cancel_before_publish_archives_without_remote_writes(task):
    store, ident, client, _ = task
    store.update(ident, status="cancelled", delivery_cancel_requested=True)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "cancelled"
    assert result["cleanup"]["local"] == "cleaned"
    assert client.pr is None
    restored = delivery_archive.restore(store, ident)
    assert (restored / "docs/guide.md").read_text() == "Correct explanation.\n"


def test_cancel_after_pr_disables_auto_merge_and_preserves_pr(task):
    store, ident, client, _ = task
    delivery.reconcile(store, ident, client=client)
    store.update(ident, delivery_cancel_requested=True)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "cancelled"
    assert client.disabled == 1 and client.pr["state"] == "open"


def test_cancel_racing_merge_reports_actual_merge(task):
    store, ident, client, _ = task
    first = delivery.reconcile(store, ident, client=client)
    client.mark_merged(first["delivery"])
    store.update(ident, delivery_cancel_requested=True)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "merged"


def test_human_disabling_auto_merge_is_respected(task):
    store, ident, client, _ = task
    delivery.reconcile(store, ident, client=client)
    client.pr["auto_merge"] = None
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "paused"
    assert client.enable_count == 1


def test_closed_pr_cleans_remote_branch_without_reopening(task):
    store, ident, client, _ = task
    delivery.reconcile(store, ident, client=client)
    client.pr.update(state="closed")
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "closed"
    assert result["cleanup"]["remote"] == "cleaned"
    assert client.creations == 1


@pytest.mark.parametrize("status", ["blocked", "failed", "interrupted"])
def test_partial_accepted_governance_publishes(task, status):
    store, ident, client, _ = task
    store.update(ident, status=status, stop_reason="budget_exhausted")
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "waiting_checks"
    assert result["status"] == status


def test_no_accepted_checkpoint_does_not_create_pr(task):
    store, ident, client, _ = task
    store.update(ident, checkpoint=None, status="not_reproduced", verified_manifest=None)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "no_changes"
    assert result["cleanup"]["local"] == "cleaned"
    assert not client.pr


def test_unknown_local_change_blocks_archive_and_deletion(task):
    store, ident, client, _ = task
    root = Path(store.get(ident)["worktree"])
    (root / "docs/guide.md").write_text("Another person's work\n")
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "blocked"
    assert result["cleanup"]["local"] == "blocked"
    assert root.exists() and not client.pr


def test_archive_corruption_blocks_restore_and_cleanup(task):
    store, ident, client, _ = task
    first = delivery.reconcile(store, ident, client=client)
    folder, _ = delivery_archive.verify_archive(store, first)
    (folder / "source/docs/guide.md").write_text("Corrupted\n")
    with pytest.raises((HarnessError, ValueError)):
        delivery_archive.restore(store, ident)


@pytest.mark.parametrize("status", ["queued", "running", "waiting_input"])
def test_active_task_is_never_published_or_cleaned(task, status):
    store, ident, client, _ = task
    store.update(ident, status=status)
    assert ident not in pending(store)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "pending"
    assert Path(result["worktree"]).exists() and client.pr is None


def test_historical_tasks_are_never_selected_or_mutated(task):
    store, ident, client, _ = task
    old = store.update(ident, pipeline_version=PIPELINE_VERSION - 1)
    assert pending(store) == []
    with pytest.raises(HarnessError, match="旧任务"):
        delivery.reconcile(store, ident, client=client, retry=True)
    assert store.get(ident) == old


def test_currently_mergeable_pr_merges_with_verified_sha(task):
    store, ident, client, _ = task
    client.ready = True
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "merged", result
    assert client.enable_count == 0


def test_approval_or_patch_tampering_blocks_publication(task):
    store, ident, client, _ = task
    attempt = store.attempts(ident)[0]
    attempt["review"]["decision"] = "rejected"
    store.save_attempt(ident, 1, attempt)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "blocked"
    assert client.pr is None


def test_commit_retry_reuses_deterministic_object(task):
    store, ident, _, _ = task
    candidate(store, ident)
    first = commit(store, ident)
    second = commit(store, ident)
    assert first == second


def test_remote_branch_change_is_not_overwritten_or_deleted(task):
    store, ident, client, repo = task
    first = delivery.reconcile(store, ident, client=client)
    branch = first["branch"]
    main = command(repo, "rev-parse", "main")
    command(client.bare, "update-ref", "refs/heads/" + branch, main)
    result = delivery.reconcile(store, ident, client=client)
    assert result["delivery"]["state"] == "blocked"
    assert command(client.bare, "rev-parse", branch) == main


def test_baseline_ignores_operator_dirty_and_staged_content(task):
    store, ident, _, repo = task
    (repo / "docs/guide.md").write_text("staged\n")
    command(repo, "add", "docs/guide.md")
    index = (repo / ".git/index").read_bytes()
    (repo / "docs/guide.md").write_text("unstaged\n")
    frozen = store.root / "jobs" / ident / "source/docs/guide.md"
    assert frozen.read_text() == "Old explanation.\n"
    assert (repo / ".git/index").read_bytes() == index
    assert (repo / "docs/guide.md").read_text() == "unstaged\n"


def advance_main(client, tmp_path, *, conflict=False):
    root = tmp_path / "upstream"
    subprocess.run(["git", "clone", str(client.bare), str(root)], check=True, capture_output=True)
    command(root, "config", "user.name", "Lingye")
    command(root, "config", "user.email", "616202172@qq.com")
    (root / ("docs/guide.md" if conflict else "docs/other.md")).write_text("Upstream change\n")
    command(root, "add", ".")
    command(root, "commit", "-m", "upstream")
    command(root, "push", "origin", "main")
    return command(root, "rev-parse", "HEAD")


def test_main_advance_is_revalidated_then_pushed_without_rewriting(task, tmp_path, monkeypatch):
    store, ident, client, _ = task
    first = delivery.reconcile(store, ident, client=client)
    old = first["delivery"]["commit_sha"]
    main = advance_main(client, tmp_path)
    calls = []

    def validate(store, task_id, root, baseline, coder, verifier, check_cancel):
        check_cancel()
        assert (root / "docs/guide.md").read_text() == "Correct explanation.\n"
        assert (root / "docs/other.md").read_text() == "Upstream change\n"
        assert (baseline / "docs/guide.md").read_text() == "Old explanation.\n"
        calls.append(True)
        approval = store.get(task_id)["publication_candidate"]
        manifest = source_manifest(root)
        store.update(task_id, publication_candidate={**approval, "manifest": manifest, "digest": manifest_digest(manifest)})

    monkeypatch.setattr("chatcopilot.harness.delivery_validation.revalidate", validate)
    result = delivery.reconcile(store, ident, client=client, coder=object(), verifier=object())
    assert result["delivery"]["state"] == "waiting_checks", result["delivery"]
    new = result["delivery"]["commit_sha"]
    assert command(client.bare, "rev-list", "--parents", "-1", new).split() == [new, old, main]
    assert client.creations == 1 and client.enable_count == 2 and client.disabled == 1
    assert calls == [True] and result["cleanup"]["local"] == "cleaned"


def test_main_conflict_stops_and_preserves_recoverable_evidence(task, tmp_path):
    store, ident, client, _ = task
    first = delivery.reconcile(store, ident, client=client)
    advance_main(client, tmp_path, conflict=True)
    result = delivery.reconcile(store, ident, client=client, coder=object(), verifier=object())
    assert result["delivery"]["state"] == "blocked"
    assert result["delivery"]["error_code"] == "delivery_merge_conflict"
    assert client.pr["auto_merge"] is None
    assert result["delivery"]["commit_sha"] == first["delivery"]["commit_sha"]
    assert result["cleanup"]["local"] == "cleaned", result["cleanup"]
    folder, _ = delivery_archive.verify_archive(store, result)
    assert "<<<<<<<" in (folder / "source/docs/guide.md").read_text()


def test_revalidation_failure_never_pushes_new_main_merge(task, tmp_path, monkeypatch):
    store, ident, client, _ = task
    first = delivery.reconcile(store, ident, client=client)
    advance_main(client, tmp_path)
    def reject(*args):
        raise HarnessError("delivery_revalidation_failed", "tests failed")
    monkeypatch.setattr("chatcopilot.harness.delivery_validation.revalidate", reject)
    result = delivery.reconcile(store, ident, client=client, coder=object(), verifier=object())
    assert result["delivery"]["state"] == "blocked"
    assert command(client.bare, "rev-parse", first["branch"]) == first["delivery"]["commit_sha"]
    assert client.enable_count == 1 and client.pr["auto_merge"] is None
    assert result["cleanup"]["local"] == "cleaned"


def test_archive_remains_downloadable_after_cleanup(task):
    from chatcopilot.harness.api import HarnessController
    store, ident, client, repo = task
    delivery.reconcile(store, ident, client=client)
    controller = HarnessController(repo, root=store.root)
    assert controller.get(ident)["candidate_available"] is True
    assert b"Correct explanation" in controller.candidate_patch(ident)


def test_old_worker_entry_cannot_even_create_a_lock(task):
    from chatcopilot.harness.delivery_runtime import run_one
    store, ident, _, _ = task
    store.update(ident, pipeline_version=PIPELINE_VERSION - 1)
    path = store.root / "jobs" / ident / "worker.lock"
    assert not path.exists()
    with pytest.raises(HarnessError, match="旧任务"):
        run_one(store, ident)
    assert not path.exists()


def test_ci_failure_is_visible_without_disabling_required_checks(task, monkeypatch):
    store, ident, client, _ = task
    monkeypatch.setattr(client, "check_summary", lambda state: {"failed": True, "rows": [{"name": "Python", "conclusion": "failure", "status": "completed"}]})
    result = delivery.reconcile(store, ident, client=client)
    assert result["status"] == "fixed" and result["delivery"]["state"] == "checks_failed"
    assert result["delivery"]["auto_merge"] is True
    assert ident in pending(store)


def test_delivery_credentials_are_not_in_the_coder_environment(task, monkeypatch):
    from chatcopilot.external_tools.codex_cli.command import build_codex_subprocess_env
    monkeypatch.setenv("CHATCOPILOT_HARNESS_GITHUB_TOKEN_FILE", "/private/github-credential")
    monkeypatch.setenv("GH_TOKEN", "unit-test-not-a-real-credential")
    env = build_codex_subprocess_env("/usr/bin/codex")
    assert "GH_TOKEN" not in env and "CHATCOPILOT_HARNESS_GITHUB_TOKEN_FILE" not in env


def test_pending_revalidation_retains_evaluation_identity(task, monkeypatch):
    from types import SimpleNamespace
    from chatcopilot.harness import delivery_validation
    from chatcopilot.harness.models import VerificationPlan, VerificationResult, VerificationCheck
    store, ident, _, repo = task
    approval = candidate(store, ident)
    record = store.get(ident)
    store.update(ident, source={"kind": "robot_task"}, publication_candidate={**approval, "profile": "fast"},
                 verification_plan=VerificationPlan(("target",), ("target",), (), 1).to_payload())
    class Checks:
        def __init__(self, *args):
            pass
        def bind(self, *args):
            pass
        def verify(self, *args):
            return {"passed": True, "checks": []}
    calls = []
    class Verifier:
        def run(self, task, candidate, run_id, checks, cancel):
            calls.append(run_id)
            if len(calls) == 1:
                raise HarnessError("result_pending", "result is not durable yet")
            return VerificationResult(run_id, candidate.digest, (VerificationCheck("target", 1, "passed"),))
        def regressions(self, *args):
            return {"passed_cases": []}
    monkeypatch.setattr(delivery_validation, "CodeHealthChecks", Checks)
    coder = SimpleNamespace(review=lambda *args: {"decision": "approved", "problem": "", "reason": "fixture", "evidence_refs": ["verification"]})
    with pytest.raises(HarnessError, match="durable"):
        delivery_validation.revalidate(store, ident, Path(record["worktree"]), repo, coder, Verifier(), lambda: None)
    assert store.get(ident)["delivery_evaluation"]["id"] == calls[0]
    assert store.get(ident).get("current_evaluation_id") is None
    delivery_validation.revalidate(store, ident, Path(record["worktree"]), repo, coder, Verifier(), lambda: None)
    assert calls[0] == calls[1]
    assert store.get(ident)["delivery_evaluation"] is None
    assert store.get(ident).get("current_evaluation_id") is None


def test_cleanup_waits_for_managed_worker_exit(task, monkeypatch):
    from types import SimpleNamespace
    store, ident, _, _ = task
    store.update(ident, dispatch_state='scheduled', unit='agentstrata-harness-' + ident[7:])
    real_run = subprocess.run
    def running(argv, **kwargs):
        if argv[0] == 'systemctl':
            return SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=active\n')
        return real_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', running)
    with pytest.raises(HarnessError, match='进程退出'):
        delivery_archive.cleanup_local(store, ident)
    assert Path(store.get(ident)['worktree']).exists()
    def stopped(argv, **kwargs):
        if argv[0] == 'systemctl':
            return SimpleNamespace(returncode=0, stdout='LoadState=not-found\nActiveState=inactive\n')
        return real_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', stopped)
    delivery_archive.cleanup_local(store, ident)
    assert store.get(ident)['cleanup']['local'] == 'cleaned'


def test_failed_creation_does_not_delete_an_unowned_matching_branch(task):
    store, ident, _, repo = task
    record = store.get(ident)
    branch, root = record['branch'], Path(record['worktree'])
    git(repo, 'worktree', 'remove', '--force', str(root))
    store.update(ident, worktree=None, status='blocked')
    delivery_archive.cleanup_local(store, ident)
    assert command(repo, 'rev-parse', branch) == record['base_commit']
    assert store.get(ident)['cleanup']['local'] == 'not_created'


def test_wrong_github_actor_blocks_without_automatic_retry(task, monkeypatch):
    store, ident, client, _ = task
    request = client.request
    def wrong_actor(method, path, **kwargs):
        if path == '/user':
            return {'login': 'different-actor', 'type': 'User'}
        return request(method, path, **kwargs)
    monkeypatch.setattr(client, 'request', wrong_actor)
    result = delivery.reconcile(store, ident, client=client)
    assert result['delivery']['state'] == 'blocked'
    assert result['delivery']['error_code'] == 'github_actor_mismatch'
    assert client.creations == 0 and ident not in pending(store)
