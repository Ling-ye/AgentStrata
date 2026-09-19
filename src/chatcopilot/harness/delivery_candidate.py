"""Turn independently accepted evidence into an exact, recoverable Git commit."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import runpy
import time
from typing import Any

from chatcopilot.core import github_transport
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.delivery_archive import verify_worktree
from chatcopilot.harness.github_delivery import git
from chatcopilot.harness.local_commit import regression_content, regression_refs
from chatcopilot.harness.models import HarnessError, acceptance_digest
from chatcopilot.harness.preparation import verification_purpose


def accepted(store: Any, task: dict[str, Any]) -> bool:
    if task.get("delivery_cancel_requested") or task["status"] in {"cancelled", "cancel_requested"}:
        return False
    receipt = task.get("accepted_candidate") or {}
    if (task.get("verification_plan") or {}).get("purpose", "repair") != verification_purpose(task["source"]):
        return False
    attempts = [a for a in store.attempts(task["task_id"]) if a.get("number") == receipt.get("attempt") and a.get("status") == "accepted"]
    if len(attempts) != 1 or receipt.get("verification_digest") != acceptance_digest(task, attempts[0]):
        return False
    return (task["status"] == "fixed" and bool(task.get("verified_digest"))
            and receipt.get("candidate_digest") == task["verified_digest"]
            and receipt.get("goal_digest") == task.get("acceptance", {}).get("sha256")
            and bool(receipt.get("verification_digest")) and bool(receipt.get("review_binding")))


def candidate(store: Any, task_id: str) -> dict[str, Any]:
    task = store.get(task_id)
    if not accepted(store, task):
        raise HarnessError("incomplete_verification", "候选未完整验收，不具备交付资格")
    if task.get("publication_candidate"):
        return task["publication_candidate"]
    root = verify_worktree(store, task)
    before = task["baseline_manifest"]
    attempts = store.attempts(task_id)
    approved = [a for a in attempts if a.get("status") == "accepted" and a.get("candidate_digest") == task["verified_digest"]]
    if len(approved) != 1 or manifest_digest(source_manifest(root)) != task["verified_digest"]:
        raise HarnessError("incomplete_verification", "修复候选缺少唯一验收身份")
    references = regression_refs(task)
    for reference in references:
        if reference["kind"] not in {"pytest", "agent_case"}:
            continue
        content = regression_content(task, reference)
        if hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise HarnessError("reproducer_changed", "冻结回归测试已变化")
        destination = root / reference["path"]
        if destination.exists() and destination.read_bytes() != content:
            raise HarnessError("regression_conflict", "回归测试路径已被其他内容占用")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)
    store.update(task_id, regression=references[0] if references else None, regressions=references)
    current = source_manifest(root)
    title = "[AI Harness] 修复已复现问题并收录回归验证"
    profile = "fast"
    if task["source"].get("kind") == "code_health":
        title = "[Code Health] " + task["source"]["governance_target"]["summary"].replace("\n", " ")[:100]
        profile = "full"
    receipt = task["accepted_candidate"]
    if len(approved) != 1 or approved[0]["number"] != receipt["attempt"] or approved[0].get("review", {}).get("binding") != receipt["review_binding"]:
        raise HarnessError("incomplete_verification", "交付凭据与审查版本不一致")
    if not approved or any(a.get("review", {}).get("decision") != "approved" or a.get("regressions") for a in approved):
        raise HarnessError("review_required", "交付必须有独立审核和完整通过的验收证据")
    paths = sorted(n for n in before.keys() | current.keys() if before.get(n) != current.get(n))
    if not paths:
        raise HarnessError("delivery_empty", "验收结果没有代码差异，不创建 PR")
    result = {"manifest": current, "digest": manifest_digest(current), "paths": paths, "title": title,
              "profile": profile, "attempts": [a["number"] for a in approved]}
    store.update(task_id, publication_candidate=result, working_digest=result["digest"])
    return result


def commit(store: Any, task_id: str) -> str:
    task = store.get(task_id)
    approval = candidate(store, task_id)
    task = store.get(task_id)
    root = verify_worktree(store, task)
    if source_manifest(root) != approval["manifest"]:
        raise HarnessError("workspace_changed", "交付源码与验收摘要不一致")
    state = task["delivery"]
    directory = private_directory(store.root / "jobs" / task_id / "publication")
    intent = task.get("publication_intent")
    env = github_transport.git_environment(author_name=state["author_name"], author_email=state["author_email"])
    if intent is None:
        parent = git(root, "rev-parse", "HEAD").decode().strip()
        if parent != task["base_commit"] or git(root, "diff", "--cached", "--name-only").strip():
            raise HarnessError("index_changed", "任务基线或暂存区已变化")
        index_env = {**env, "GIT_INDEX_FILE": str(directory / "index")}
        git(root, "read-tree", parent, env=index_env)
        updates = []
        policy_root = Path(__file__).resolve().parents[3]
        scan = runpy.run_path(str(policy_root / "scripts/check_public_repo.py"))["scan_text"]
        for name in approval["paths"]:
            record = approval["manifest"].get(name)
            if record is None:
                updates.append(b"0 " + b"0" * 40 + b"\t" + os.fsencode(name) + b"\0")
                continue
            raw = (root / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise HarnessError("workspace_changed", "提交构造期间文件变化")
            if scan(raw.decode("utf-8", errors="replace"), path=name):
                raise HarnessError("publication_private_data", "交付包含不允许公开的信息")
            blob = git(root, "hash-object", "-w", "--no-filters", "--stdin", data=raw, env=index_env).strip()
            updates.append((b"100755" if record["executable"] else b"100644") + b" " + blob + b"\t" + os.fsencode(name) + b"\0")
        git(root, "update-index", "-z", "--index-info", data=b"".join(updates), env=index_env)
        tree = git(root, "write-tree", env=index_env).decode().strip()
        message = approval["title"] + "\n\nGenerated-by: AI Harness\nHarness-Task: " + task_id + "\n"
        if scan(state["author_name"] + " <" + state["author_email"] + ">\n" + message, path="commit"):
            raise HarnessError("commit_identity_invalid", "提交身份或说明未通过公开检查")
        index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        intent = {"parent": parent, "tree": tree, "message": message, "date": f"{int(time.time())} +0000",
                  "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest()}
        store.update(task_id, publication_intent=intent)
    env.update(GIT_AUTHOR_DATE=intent["date"], GIT_COMMITTER_DATE=intent["date"])
    sha = git(root, "commit-tree", intent["tree"], "-p", intent["parent"], data=intent["message"].encode(), env=env).decode().strip()
    # Record the deterministic object before the ref mutation; interrupted updates are recoverable.
    store.update(task_id, publication_intent={**intent, "sha": sha})
    head = git(root, "rev-parse", "HEAD").decode().strip()
    index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
    if hashlib.sha256(index_path.read_bytes()).hexdigest() != intent["index_sha256"]:
        if head != sha or git(root, "write-tree").decode().strip() != intent["tree"]:
            raise HarnessError("index_changed", "提交期间暂存区出现外部修改")
    if head == intent["parent"]:
        git(root, "update-ref", "refs/heads/" + task["branch"], sha, head)
    elif head != sha:
        raise HarnessError("workspace_changed", "提交期间分支发生外部变化")
    git(root, "read-tree", sha)
    if source_manifest(root) != approval["manifest"] or git(root, "diff", "HEAD", "--name-only").strip():
        raise HarnessError("workspace_changed", "提交后的源码与验收结果不符")
    store.update(task_id, working_head=sha, delivery={**state, "state": "committed", "commit_sha": sha,
                 "tree_sha": intent["tree"], "candidate_digest": approval["digest"]})
    return sha


def pr_body(task: dict[str, Any]) -> str:
    approval = task["publication_candidate"]
    return ("## 问题与修改\n\n" + approval["title"] + "。\n\n"
            + "\n".join("- `" + p + "`" for p in approval["paths"])
            + "\n\n## 验证\n\n宿主已核验冻结证据与独立审核，验收范围：`" + approval["profile"]
            + "`。GitHub CI 和合并状态以本 PR 当前检查为准。\n\n"
            + ("本 PR 处理一个连贯治理主题；宿主核对了前后改善证据与行为保持检查。其他发现保留在私有治理报告中。\n\n"
               if task["source"].get("kind") == "code_health" else "")
            + "## 来源\n\n由 AI Harness 自动生成；详细原始记录保留在操作者私有任务档案中。\n"
            + f"\n<!-- agentstrata-harness:{task['task_id']} -->\n")
