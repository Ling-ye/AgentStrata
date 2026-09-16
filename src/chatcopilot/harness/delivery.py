"""Durable PR delivery and reconciliation, independent of repair success state."""
from __future__ import annotations

from pathlib import Path
import hashlib
import time
from typing import Any

from chatcopilot.core import github_transport
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest, verify_copy
from chatcopilot.harness import delivery_archive, delivery_candidate
from chatcopilot.harness.delivery_checks import publication_checks
from chatcopilot.harness.github_delivery import DeliveryConfig, GitHubDelivery, git
from chatcopilot.harness.models import ACTIVE, Cancelled, HarnessError, PIPELINE_VERSION, safe_error
from chatcopilot.harness.workspace import prepare

PENDING = frozenset({"pending", "committed", "pushed", "pr_open", "waiting_checks", "checks_failed", "updating", "retryable", "cancel_pending"})


def remote_baseline(repository: Path, settings: dict[str, str]) -> dict[str, Any]:
    try:
        return GitHubDelivery(DeliveryConfig.load(settings)).preflight(repository)
    except github_transport.GitHubError as exc:
        raise HarnessError(exc.code, str(exc)) from exc


def initialize(store: Any, task_id: str, client: GitHubDelivery | None = None) -> None:
    task = store.get(task_id)
    if not task.get("delivery") or task.get("pipeline_version") != PIPELINE_VERSION:
        raise HarnessError("source_archived", "旧任务只读保留，请创建新任务")
    if task.get("baseline_manifest"):
        return
    client = client or GitHubDelivery(DeliveryConfig.load())
    state = task["delivery"]
    client.verify_target(state)
    repository = Path(task["repository"])
    directory = private_directory(store.root / "jobs" / task_id)
    client.fetch(repository, directory, state["base_sha"])
    root = prepare(repository, store.root, task_id, state["base_sha"])
    manifest = source_manifest(root)
    store.update(task_id, worktree=str(root), branch="feat/harness-" + task_id[7:], working_head=state["base_sha"],
                 working_digest=manifest_digest(manifest), delivery={**state, "branch": "feat/harness-" + task_id[7:]})
    frozen = directory / "source"
    if frozen.exists():
        verify_copy(frozen, manifest)
    else:
        copy_sources(root, frozen, manifest)
    if source_manifest(root) != manifest:
        raise HarnessError("snapshot_failed", "冻结期间任务源码变化")
    source = task["source"]
    if source.get("kind", "evaluation") == "code_health":
        source = {**source, "snapshot_digest": manifest_digest(manifest), "original_branch": "main"}
    store.update(task_id, baseline_manifest=manifest, source=source)


def _save(store: Any, task_id: str, **fields: Any) -> dict[str, Any]:
    current = store.get(task_id)
    state = {**current["delivery"], **fields, "updated_at": time.time()}
    store.update(task_id, delivery=state)
    return state


def _poll_cancel(store: Any, task_id: str) -> None:
    task = store.get(task_id)
    if task.get("delivery_cancel_requested") or task["status"] in {"cancelled", "cancel_requested"}:
        raise Cancelled()


def _cleanup(store: Any, task_id: str) -> None:
    try:
        delivery_archive.cleanup_local(store, task_id)
    except Exception as exc:
        task = store.get(task_id)
        store.update(task_id, cleanup={**task.get("cleanup", {}),
            "local": "pending" if getattr(exc, "code", "") == "cleanup_active" else "blocked", "error": safe_error(exc)})


def _pr_receipt(store: Any, task_id: str, pr: dict[str, Any]) -> dict[str, Any]:
    return _save(store, task_id, pr_number=pr["number"], pr_url=pr["html_url"], pr_node_id=pr["node_id"], state="pr_open")


def _update_main(store: Any, task_id: str, client: GitHubDelivery, main: str, coder: Any, verifier: Any) -> None:
    from chatcopilot.harness.delivery_validation import revalidate
    task = store.get(task_id)
    state = task["delivery"]
    inflight = bool(task.get("current_evaluation_id") and task.get("delivery_evaluation"))
    if inflight and state.get("update_base_sha") != main:
        raise HarnessError("delivery_update_in_flight", "原主干复测仍在执行，先完成或取消该测评")
    # Persist ownership of the disable request, so retry does not mistake it for a human action.
    _save(store, task_id, state="updating", update_base_sha=main, disabling_for_update=True)
    pr = client.pull(state["pr_number"])
    if pr.get("auto_merge"):
        client.disable(state)
    _poll_cancel(store, task_id)
    state = _save(store, task_id, auto_merge=False)
    root = delivery_archive.restore(store, task_id)
    repository, directory, _, branch = delivery_archive.task_paths(store, store.get(task_id))
    client.fetch(repository, directory, main)
    if not inflight and git(root, "diff", "--cached", "--name-only").strip():
        raise HarnessError("index_changed", "主干更新前暂存区出现外部修改")
    if not inflight and (git(root, "diff", "HEAD", "--name-only").strip() or git(root, "ls-files", "--others", "--exclude-standard").strip()):
        # Resume a failed update from the last published commit, after preserving the exact owned bytes.
        delivery_archive.archive(store, task_id)
        extras = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        git(root, "reset", "--hard", state["commit_sha"])
        for raw in extras:
            if raw:
                path = root / raw.decode()
                if path.exists():
                    path.unlink()
        store.update(task_id, working_digest=manifest_digest(source_manifest(root)))
    baseline = directory / "revalidation-base"
    if baseline.exists():
        if (baseline.resolve() != baseline or git(baseline, "rev-parse", "--path-format=absolute", "--git-common-dir")
                != git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
                or git(baseline, "rev-parse", "HEAD").decode().strip() != main or git(baseline, "status", "--porcelain").strip()):
            raise HarnessError("delivery_update_interrupted", "主干验证工作区变化，保留现场")
    else:
        git(repository, "worktree", "add", "--detach", str(baseline), main)
    env = github_transport.git_environment(author_name=state["author_name"], author_email=state["author_email"])
    try:
        try:
            if not inflight:
                git(root, "merge", "--no-commit", "--no-ff", main, env=env)
        except HarnessError as exc:
            # Preserve conflict bytes in the verified archive; do not resolve with a model implicitly.
            store.update(task_id, working_digest=manifest_digest(source_manifest(root)))
            raise HarnessError("delivery_merge_conflict", "最新 main 与任务变更冲突，已停止自动交付") from exc
        store.update(task_id, working_digest=manifest_digest(source_manifest(root)))
        index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        index_digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
        if inflight and index_digest != task["delivery"].get("update_index_sha256"):
            raise HarnessError("index_changed", "等待复测期间暂存区变化")
        _save(store, task_id, update_index_sha256=index_digest)
        revalidate(store, task_id, root, baseline, coder, verifier, lambda: _poll_cancel(store, task_id))
        _poll_cancel(store, task_id)
        if hashlib.sha256(index_path.read_bytes()).hexdigest() != index_digest:
            raise HarnessError("index_changed", "主干复验期间暂存区出现外部修改")
        tree = git(root, "write-tree").decode().strip()
        # This is a task-branch merge, while the final main integration remains squash/linear.
        sha = git(root, "commit-tree", tree, "-p", state["commit_sha"], "-p", main,
                  data=b"Sync main and repeat Harness acceptance\n", env=env).decode().strip()
        _save(store, task_id, update_commit_sha=sha, update_tree_sha=tree)
        git(root, "update-ref", "refs/heads/" + branch, sha, state["commit_sha"])
        git(root, "reset", "--mixed", "--quiet", sha)
        store.update(task_id, working_head=sha)
        _poll_cancel(store, task_id)
        approval = store.get(task_id)["publication_candidate"]
        _save(store, task_id, commit_sha=sha, tree_sha=tree, base_sha=main, state="pr_open", auto_merge=False,
              candidate_digest=approval["digest"], push_previous=state["commit_sha"], update_base_sha=None)
        publication_checks(store, task_id, lambda: _poll_cancel(store, task_id))
        client.push(root, directory, branch, sha, previous=state["commit_sha"])
        _save(store, task_id, push_previous=None, disabling_for_update=False)
    finally:
        git(repository, "worktree", "remove", "--force", str(baseline))


def reconcile(store: Any, task_id: str, *, client: GitHubDelivery | None = None, coder: Any = None,
              verifier: Any = None, retry: bool = False, cleanup_only: bool = False) -> dict[str, Any]:
    task = store.get(task_id)
    if task.get("pipeline_version") != PIPELINE_VERSION or not task.get("delivery"):
        raise HarnessError("source_archived", "旧任务只读保留")
    if task["status"] in {*ACTIVE, "waiting_input"} or (task.get("current_evaluation_id") and not task.get("delivery_evaluation")):
        return task
    if cleanup_only:
        _cleanup(store, task_id)
        return store.get(task_id)
    state = task["delivery"]
    if state["state"] not in PENDING and not retry:
        return task
    try:
        _reconcile(store, task_id, client=client, coder=coder, verifier=verifier)
    except Cancelled:
        _save(store, task_id, state="cancel_pending", error_code=None, message="等待核对 PR 并关闭自动合并")
    except Exception as exc:
        transient = isinstance(exc, github_transport.GitHubError) and (exc.code == "github_unavailable" or exc.status == 429 or exc.status >= 500)
        _save(store, task_id, state="retryable" if transient else "blocked", error_code=getattr(exc, "code", "delivery_failed"),
              message=safe_error(exc))
    finally:
        _cleanup(store, task_id)
    return store.get(task_id)


def _reconcile(store: Any, task_id: str, *, client: GitHubDelivery | None, coder: Any, verifier: Any) -> None:
    task = store.get(task_id)
    state = task["delivery"]
    repository, directory, root, branch = delivery_archive.task_paths(store, task)
    if state.get("branch", branch) != branch:
        raise HarnessError("delivery_target_changed", "交付分支不属于本任务")
    cancelled = task.get("delivery_cancel_requested") or task["status"] == "cancelled"
    if cancelled and task.get("current_evaluation_id") and task.get("delivery_evaluation"):
        if verifier is None:
            raise HarnessError("delivery_validator_unavailable", "取消复测需要 Evaluation 适配器")
        evaluator = verifier.evaluator
        evaluator.cancel(task["current_evaluation_id"])
        record = evaluator.client.get(task["current_evaluation_id"])
        if record["status"] in {"queued", "running"}:
            _save(store, task_id, state="cancel_pending", message="等待受管复测进程退出")
            return
        store.update(task_id, current_evaluation_id=None, delivery_evaluation=None)
    if cancelled and not state.get("pr_number") and not state.get("commit_sha"):
        _save(store, task_id, state="cancelled", message="已取消交付")
        return
    if not cancelled and not state.get("commit_sha") and not delivery_candidate.accepted(store, task):
        _save(store, task_id, state="no_changes", message="没有可交付的验收成果")
        return
    client = client or GitHubDelivery(DeliveryConfig.load())
    client.verify_target(state)
    if not state.get("commit_sha"):
        if not root.exists():
            root = delivery_archive.restore(store, task_id)
        _poll_cancel(store, task_id)
        delivery_candidate.commit(store, task_id)
        task = store.get(task_id)
        state = task["delivery"]
    if not state.get("pr_number"):
        # Query by deterministic branch even after cancellation: the previous create may have succeeded.
        rows = client.request("GET", client.repo + "/pulls", params={"state": "all", "head": state["repository"].split('/')[0] + ':' + branch, "per_page": 100})
        if not isinstance(rows, list) or len(rows) > 1:
            raise HarnessError("delivery_pr_ambiguous", "PR 查询结果不唯一")
        if rows:
            client.verify_pr(rows[0], state)
            state = _pr_receipt(store, task_id, rows[0])
        elif cancelled:
            _save(store, task_id, state="cancelled", message="已取消；未创建 PR")
            return
        else:
            if not root.exists():
                root = delivery_archive.restore(store, task_id)
            _poll_cancel(store, task_id)
            publication_checks(store, task_id, lambda: _poll_cancel(store, task_id))
            client.push(root, directory, branch, state["commit_sha"])
            state = _save(store, task_id, state="pushed")
            _poll_cancel(store, task_id)
            pr = client.ensure_pr(state, task["publication_candidate"]["title"], delivery_candidate.pr_body(task))
            state = _pr_receipt(store, task_id, pr)
    if state.get("push_previous"):
        _poll_cancel(store, task_id)
        if not root.exists():
            root = delivery_archive.restore(store, task_id)
        publication_checks(store, task_id, lambda: _poll_cancel(store, task_id))
        client.push(root, directory, branch, state["commit_sha"], previous=state["push_previous"])
        state = _save(store, task_id, push_previous=None, disabling_for_update=False)
    pr = client.pull(state["pr_number"])
    client.verify_pr(pr, state)
    if pr.get("merged") or pr["state"] == "closed":
        merged = bool(pr.get("merged"))
        state = _save(store, task_id, state="merged" if merged else "closed", auto_merge=False,
                      merge_sha=pr.get("merge_commit_sha") if merged else None, message="已合并到远端 main" if merged else "PR 已关闭")
        client.delete_branch(state, repository, directory)
        current = store.get(task_id)
        store.update(task_id, cleanup={**current.get("cleanup", {}), "remote": "cleaned"})
        return
    if cancelled:
        if pr.get("auto_merge"):
            client.disable(state)
        _save(store, task_id, state="cancelled", auto_merge=False, message="自动合并已关闭，PR 保留")
        return
    if state.get("auto_merge") and not pr.get("auto_merge") and not state.get("disabling_for_update"):
        _save(store, task_id, state="paused", auto_merge=False, message="自动合并已被外部关闭，等待人工处理")
        return
    main = client.request("GET", client.repo + "/branches/main")["commit"]["sha"]
    if main != state["base_sha"]:
        if coder is None or verifier is None:
            raise HarnessError("delivery_validator_unavailable", "主干更新需要冻结验证与独立审核器")
        _update_main(store, task_id, client, main, coder, verifier)
        state = store.get(task_id)["delivery"]
        pr = client.pull(state["pr_number"])
        client.verify_pr(pr, state)
    _poll_cancel(store, task_id)
    if not pr.get("auto_merge"):
        # Record the desired action before the request; response-loss recovery observes the PR itself.
        _save(store, task_id, enabling_auto_merge=True)
        if client.merge_ready(state):
            client.merge(state)
        else:
            client.enable(state)
    pr = client.pull(state["pr_number"])
    client.verify_pr(pr, state)
    checks = client.check_summary(state)
    _save(store, task_id, state="checks_failed" if checks["failed"] else "waiting_checks", checks=checks["rows"],
          auto_merge=bool(pr.get("auto_merge")), enabling_auto_merge=False, error_code=None,
          message="CI 未通过，自动合并受阻；保留 PR 等待检查恢复" if checks["failed"] else "等待 GitHub 检查、审查及自动合并")
    if pr.get("merged"):
        _reconcile(store, task_id, client=client, coder=coder, verifier=verifier)
