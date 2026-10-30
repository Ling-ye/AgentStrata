"""Freeze the reviewed checkout without committing or changing its index."""
from pathlib import Path
import hashlib
import os
import subprocess

from chatcopilot.core.private_sqlite import private_directory, repair_owned_private_directory
from chatcopilot.core.source_snapshot import copy_sources, git_output, manifest_digest, source_manifest, verify_copy
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.workspace import prepare


def baseline(repository: Path) -> dict:
    from chatcopilot.core.application_installation import application_source
    selected = application_source(repository)
    source, manifest = selected if selected else (repository, source_manifest(repository))
    return {"mode": "local", "state": "local", "base_sha": git_output(repository, "rev-parse", "HEAD"),
            "source_digest": manifest_digest(manifest), **({"source_root": str(source)} if selected else {})}


def initialize(store, task_id):
    task = store.get(task_id)
    if task.get("baseline_manifest"):
        return
    repository = Path(task["repository"])
    state = task["delivery"]
    directory = private_directory(store.root / "jobs" / task_id)
    frozen = directory / "source"
    source = repository
    if state.get("source_root"):
        from chatcopilot.core.application_installation import application_source
        selected = application_source(repository, task["source"].get("bot_id"))
        if selected is None or str(selected[0]) != state["source_root"]:
            raise HarnessError("workspace_changed", "应用运行版本在提交后发生变化，请重新发起任务")
        source, manifest = selected
    else:
        manifest = source_manifest(repository)
    if manifest_digest(manifest) != state["source_digest"]:
        raise HarnessError("workspace_changed", "提交后源码发生变化；请重新发起需求任务")
    if frozen.exists():
        verify_copy(frozen, manifest)
    else:
        copy_sources(source, frozen, manifest)
    root = prepare(repository, store.root, task_id, state["base_sha"])
    repair_owned_private_directory(root)
    # Only this task's freshly allocated worktree is replaced. The operator's
    # checkout/index and all private/runtime data remain outside this copy.
    for name in source_manifest(root):
        (root / name).unlink()
    copy_sources(frozen, root, manifest)
    if source_manifest(root) != manifest:
        raise HarnessError("snapshot_failed", "需求工作区与冻结源码不一致")
    store.update(task_id, worktree=str(root), branch=git_output(root, "branch", "--show-current"),
        working_head=state["base_sha"], working_digest=state["source_digest"], baseline_manifest=manifest)


def prepare_upstream(store, task_id, check_cancel):
    """Read a pinned public repository; never execute its code or mutate the remote."""
    from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope, validate_adapter_approval
    from chatcopilot.core.install_artifacts import extract_archive, sha256
    task = store.get(task_id)
    source = task["source"]
    envelope = source.get("source_envelope")
    if not envelope:
        return
    approved = AdapterApprovalEnvelope(**{key: value for key, value in envelope.items() if key != "schema"})
    if validate_adapter_approval(approved) or approved.candidate_digest != source["source_envelope_digest"]:
        raise HarnessError("source_changed", "外部源码批准包络已变化")
    directory = private_directory(store.root / "jobs" / task_id / "upstream")
    target = directory / "source"
    recorded = task.get("upstream_source")
    if recorded:
        verify_copy(target, recorded["manifest"])
        return
    repository = directory / "git"
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0"}
    environment = {key: value for key, value in environment.items()
                   if not key.startswith("GIT_") or key in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT"}}
    def git(*args):
        check_cancel()
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=never",
            "-c", "protocol.ext.allow=never", *args], cwd=directory, env=environment,
            capture_output=True, text=True, timeout=60)
        check_cancel()
        if result.returncode:
            raise HarnessError("upstream_unavailable", "固定上游源码读取失败：" + result.stderr[-800:])
        return result.stdout.strip()
    if not repository.exists():
        git("init", "--bare", str(repository))
    git("--git-dir", str(repository), "fetch", "--depth=1", approved.source_url, approved.approved_ref)
    actual = git("--git-dir", str(repository), "rev-parse", "FETCH_HEAD^{commit}")
    if actual != approved.approved_ref:
        raise HarnessError("source_changed", "外部源码版本与批准 commit 不符")
    archive = directory / "source.tar"
    git("--git-dir", str(repository), "archive", "--format=tar", "--output=" + str(archive), actual)
    # A crash before publication leaves an untrusted partial extraction. A new
    # empty directory is used; only a complete manifest is published to roles.
    import tempfile
    import shutil
    pending = Path(tempfile.mkdtemp(prefix="extract-", dir=directory))
    try:
        extract_archive(archive, pending, strip_root=False)
        manifest = {str(path.relative_to(pending)): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "executable": bool(path.stat().st_mode & 0o111)} for path in pending.rglob("*") if path.is_file()}
        if target.exists():
            if target.is_symlink():
                raise HarnessError("source_changed", "上游源码目录被替换")
            shutil.rmtree(target)
        pending.replace(target)
        observed = {"root": str(target), "commit": actual,
                    "archive_sha256": sha256(archive), "digest": manifest_digest(manifest)}
        store.update(task_id, upstream_source={**observed, "manifest": manifest},
                     source={**source, "upstream_source": observed})
    finally:
        if pending.exists():
            shutil.rmtree(pending)

