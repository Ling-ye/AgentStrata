from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from chatcopilot.contracts.code_tasks import validate_code_task_title
from chatcopilot.contracts.tools import ToolHandlerError
from chatcopilot.core.jobs import write_json_atomic
from chatcopilot.external_tools.dev import code_task_delivery as delivery


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Source Author")
    _git(source, "config", "user.email", "source@example.invalid")
    (source / "src").mkdir()
    (source / "docs").mkdir()
    (source / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "docs" / "readme.md").write_text("public\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "baseline")

    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare")
    _git(source, "remote", "add", "origin", "git@github.com:acme/project.git")
    _git(source, "remote", "add", "test-remote", bare.as_uri())
    _git(source, "push", "test-remote", "main")
    return source, bare


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: Path,
    bare: Path,
    *,
    allowed: str = "src/**",
) -> Path:
    token_file = tmp_path / "github.token"
    token_file.write_text("github_pat_unit_test_token_123456\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv(
        "CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY",
        "acme/project",
    )
    monkeypatch.setenv(
        "CHATCOPILOT_CODE_TASK_GITHUB_TOKEN_FILE",
        str(token_file),
    )
    monkeypatch.setenv(
        "CHATCOPILOT_CODE_TASK_GIT_AUTHOR_NAME",
        "AgentStrata Bot",
    )
    monkeypatch.setenv(
        "CHATCOPILOT_CODE_TASK_GIT_AUTHOR_EMAIL",
        "bot@example.invalid",
    )
    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_DEV_ALLOWED_PATHS", allowed)
    monkeypatch.setenv(
        "CHATCOPILOT_DEV_DENIED_PATHS",
        "**/local.env,**/.git/**",
    )
    monkeypatch.setattr(
        delivery,
        "_github_git_url",
        lambda _config: bare.as_uri(),
    )
    return token_file


def _fake_github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: list[dict[str, Any]] | None = None,
    bare: Path | None = None,
    created_head: str | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def request(
        _config: delivery.GitHubDeliveryConfig,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        calls.append({"method": method, "path": path, **kwargs})
        if path.endswith("/pulls") and method == "GET":
            items = [dict(item) for item in (existing or [])]
            for item in items:
                head = item.get("head")
                if not isinstance(head, dict) or head.get("sha") != "__REMOTE__":
                    continue
                assert bare is not None
                raw_head = str((kwargs.get("params") or {}).get("head") or "")
                branch = raw_head.split(":", 1)[-1]
                item["head"] = {
                    **head,
                    "sha": _git(bare, "rev-parse", f"refs/heads/{branch}"),
                }
            return items
        if path.endswith("/pulls") and method == "POST":
            branch = str((kwargs.get("json_body") or {}).get("head") or "")
            head_sha = created_head
            if head_sha is None:
                assert bare is not None
                head_sha = _git(bare, "rev-parse", f"refs/heads/{branch}")
            return {
                "number": 7,
                "html_url": "https://github.com/acme/project/pull/7",
                "draft": True,
                "state": "open",
                "head": {"sha": head_sha},
            }
        return {"default_branch": "main"}

    monkeypatch.setattr(delivery, "_github_request", request)
    return calls


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o700])
def test_delivery_token_requires_exact_0600_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    source, bare = _repository(tmp_path)
    token_file = _configure(monkeypatch, tmp_path, source, bare)
    token_file.chmod(mode)

    with pytest.raises(ToolHandlerError, match="mode 0600"):
        delivery.load_delivery_config()


def test_delivery_token_rejects_symlink_and_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bare = _repository(tmp_path)
    token_file = _configure(monkeypatch, tmp_path, source, bare)
    token_link = tmp_path / "github-link.token"
    token_link.symlink_to(token_file)
    monkeypatch.setenv(
        "CHATCOPILOT_CODE_TASK_GITHUB_TOKEN_FILE",
        str(token_link),
    )
    with pytest.raises(ToolHandlerError):
        delivery.load_delivery_config()

    monkeypatch.setenv(
        "CHATCOPILOT_CODE_TASK_GITHUB_TOKEN_FILE",
        str(token_file),
    )
    hardlink = tmp_path / "github-hardlink.token"
    hardlink.hardlink_to(token_file)
    with pytest.raises(ToolHandlerError, match="single-link"):
        delivery.load_delivery_config()


def test_remote_git_environment_uses_ephemeral_token_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bare = _repository(tmp_path)
    token_file = _configure(monkeypatch, tmp_path, source, bare)
    config = delivery.load_delivery_config()
    original_token = config.token
    token_file.write_text("github_pat_replaced_after_load_123456\n", encoding="utf-8")
    job_dir = tmp_path / "job-token-env"
    job_dir.mkdir(mode=0o700)

    with delivery._remote_git_environment(config, job_dir) as env:
        ephemeral = Path(
            env["CHATCOPILOT_CODE_TASK_GITHUB_TOKEN_FILE"]
        )
        helper = Path(env["GIT_ASKPASS"])
        assert ephemeral != token_file
        assert ephemeral.read_text(encoding="utf-8").strip() == original_token
        assert ephemeral.stat().st_mode & 0o777 == 0o600
        assert helper.stat().st_mode & 0o777 == 0o700

    assert not ephemeral.exists()
    assert not helper.exists()


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, list[dict[str, Any]]]:
    source, bare = _repository(tmp_path)
    _configure(monkeypatch, tmp_path, source, bare)
    calls = _fake_github(monkeypatch, bare=bare)
    job_dir = tmp_path / "job_20260729_test"
    job_dir.mkdir(mode=0o700)
    worktree = job_dir / "worktree"
    delivery.prepare_delivery_worktree(
        job_dir=job_dir,
        worktree=worktree,
        instance_id="lingye-copilot-qq",
        source_root=source,
    )
    return source, bare, job_dir, calls


@pytest.mark.parametrize(
    "title",
    [
        "",
        "plain english",
        "修复标题\n第二行",
        "修复 /tmp/private/file",
        "修复 https://private.invalid/path",
        "修复 token=secret-value",
        "修复 Bearer abcdefghijk",
        "修复 sk-abcdefghijk",
        "修复 ghp_abcdefghijk",
        "修复 github_pat_abcdefghijk",
    ],
)
def test_public_title_rejects_non_public_metadata(title: str) -> None:
    with pytest.raises(ToolHandlerError):
        validate_code_task_title(title)


def test_prepare_uses_clean_clone_and_recovers_partial_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bare = _repository(tmp_path)
    _configure(monkeypatch, tmp_path, source, bare)
    _fake_github(monkeypatch)
    (source / "src" / "app.py").write_text("OWNER_DIRTY = True\n", encoding="utf-8")
    job_dir = tmp_path / "job_20260729_partial"
    job_dir.mkdir(mode=0o700)
    worktree = job_dir / "worktree"
    worktree.mkdir()
    (worktree / "partial.marker").write_text("partial\n", encoding="utf-8")

    state = delivery.prepare_delivery_worktree(
        job_dir=job_dir,
        worktree=worktree,
        instance_id="lingye-copilot-qq",
        source_root=source,
    )

    assert state["branch"] == "codex/lingye-copilot-qq/job_20260729_partial"
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (worktree / "partial.marker").exists()
    assert not (source / "git-home").exists()
    persisted = json.loads(
        (job_dir / delivery.DELIVERY_FILENAME).read_text(encoding="utf-8")
    )
    assert "title" not in persisted
    assert "changed_files" not in persisted
    assert "checks" not in persisted


def test_prepare_rejects_origin_mismatch_and_invalid_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bare = _repository(tmp_path)
    _configure(monkeypatch, tmp_path, source, bare)
    _fake_github(monkeypatch)
    _git(source, "remote", "set-url", "origin", "git@github.com:other/project.git")
    job_dir = tmp_path / "job_origin"
    job_dir.mkdir(mode=0o700)

    with pytest.raises(ToolHandlerError) as mismatch:
        delivery.prepare_delivery_worktree(
            job_dir=job_dir,
            worktree=job_dir / "worktree",
            instance_id="bot",
            source_root=source,
        )
    assert mismatch.value.error_code == "code_task_source_origin_mismatch"

    _git(source, "remote", "set-url", "origin", "git@github.com:acme/project.git")
    worktree = job_dir / "worktree"
    worktree.mkdir()
    marker = worktree / "keep"
    marker.write_text("recoverable\n", encoding="utf-8")
    (job_dir / delivery.DELIVERY_FILENAME).write_text("{broken", encoding="utf-8")
    with pytest.raises(ToolHandlerError) as invalid:
        delivery.prepare_delivery_worktree(
            job_dir=job_dir,
            worktree=worktree,
            instance_id="bot",
            source_root=source,
        )
    assert invalid.value.error_code == "code_task_delivery_state_invalid"
    assert marker.is_file()


def test_delivery_commits_pushes_and_opens_private_safe_draft_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, bare, job_dir, calls = _prepare(tmp_path, monkeypatch)
    worktree = job_dir / "worktree"
    target = worktree / "src" / "app.py"
    target.write_text("VALUE = 2\n", encoding="utf-8")
    tree_sha = delivery.compute_delivery_tree(
        job_dir=job_dir,
        worktree=worktree,
        changed_files=["src/app.py"],
        stage="validating",
    )

    result = delivery.deliver_pull_request(
        job_dir=job_dir,
        worktree=worktree,
        title="修复代码任务交付",
        changed_files=["src/app.py"],
        checks=["quick", "full", "/tmp/private/check"],
        validated_tree_sha=tree_sha,
    )

    assert result["delivered"] is True
    assert result["draft"] is True
    assert _git(worktree, "log", "-1", "--format=%s") == "修复代码任务交付"
    assert _git(worktree, "log", "-1", "--format=%an") == "AgentStrata Bot"
    assert _git(bare, "rev-parse", f"refs/heads/{result['branch']}") == result["commit_sha"]
    create = next(
        call for call in calls
        if call["method"] == "POST" and call["path"].endswith("/pulls")
    )
    body = create["json_body"]["body"]
    assert create["json_body"]["draft"] is True
    assert create["json_body"]["title"] == "修复代码任务交付"
    assert "/tmp/private" not in body
    assert "src/app.py" not in body
    assert "github_pat_" not in body
    persisted = json.loads(
        (job_dir / delivery.DELIVERY_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted["tree_sha"] == tree_sha
    assert persisted["draft"] is True
    assert set(persisted) <= delivery._DELIVERY_STATE_KEYS


def test_delivery_recovers_commit_before_state_write_and_finalizes_without_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, _bare, job_dir, _calls = _prepare(tmp_path, monkeypatch)
    worktree = job_dir / "worktree"
    (worktree / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    tree_sha = delivery.compute_delivery_tree(
        job_dir=job_dir,
        worktree=worktree,
        changed_files=["src/app.py"],
        stage="validating",
    )
    _git(worktree, "config", "user.name", "AgentStrata Bot")
    _git(worktree, "config", "user.email", "bot@example.invalid")
    _git(worktree, "add", "src/app.py")
    _git(worktree, "commit", "-m", "恢复提交交付")
    write_json_atomic(
        job_dir / "changes.json",
        {"files": [{"path": "src/app.py"}]},
    )
    write_json_atomic(
        job_dir / "validation.json",
        {
            "status": "passed",
            "checks": ["quick", "full"],
            "validated_tree_sha": tree_sha,
        },
    )

    assert delivery.delivery_retry_pending(job_dir) is True
    result = delivery.deliver_pull_request(
        job_dir=job_dir,
        worktree=worktree,
        title="恢复提交交付",
        changed_files=["src/app.py"],
        checks=["quick", "full"],
        validated_tree_sha=tree_sha,
    )
    base_sha = json.loads(
        (job_dir / delivery.DELIVERY_FILENAME).read_text(encoding="utf-8")
    )["base_sha"]
    assert _git(worktree, "rev-list", "--count", f"{base_sha}..HEAD") == "1"

    shutil.rmtree(worktree)
    assert delivery.delivery_retry_pending(job_dir) is True
    finalized = delivery.deliver_pull_request(
        job_dir=job_dir,
        worktree=worktree,
        title="恢复提交交付",
        changed_files=["src/app.py"],
        checks=["quick", "full"],
        validated_tree_sha=tree_sha,
    )
    assert finalized["commit_sha"] == result["commit_sha"]
    assert finalized["pr_url"] == result["pr_url"]


def test_delivery_rejects_mode_drift_scope_violation_and_ready_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _bare, job_dir, _calls = _prepare(tmp_path, monkeypatch)
    worktree = job_dir / "worktree"
    target = worktree / "src" / "app.py"
    target.write_text("VALUE = 4\n", encoding="utf-8")
    tree_sha = delivery.compute_delivery_tree(
        job_dir=job_dir,
        worktree=worktree,
        changed_files=["src/app.py"],
        stage="validating",
    )
    target.chmod(0o755)
    with pytest.raises(ToolHandlerError) as drift:
        delivery.deliver_pull_request(
            job_dir=job_dir,
            worktree=worktree,
            title="拒绝模式漂移",
            changed_files=["src/app.py"],
            checks=["quick", "full"],
            validated_tree_sha=tree_sha,
        )
    assert drift.value.error_code == "code_task_delivery_tree_mismatch"

    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_DEV_ALLOWED_PATHS", "src/**")
    with pytest.raises(ToolHandlerError) as scope:
        delivery.validate_delivery_paths(["docs/readme.md"], stage="validating")
    assert scope.value.error_code == "code_task_scope_violation"

    target.chmod(0o644)
    _fake_github(
        monkeypatch,
        bare=_bare,
        existing=[
            {
                "number": 8,
                "html_url": "https://github.com/acme/project/pull/8",
                "draft": False,
                "state": "open",
                "head": {"sha": "__REMOTE__"},
            }
        ],
    )
    with pytest.raises(ToolHandlerError) as ready:
        delivery.deliver_pull_request(
            job_dir=job_dir,
            worktree=worktree,
            title="拒绝非草稿请求",
            changed_files=["src/app.py"],
            checks=["quick", "full"],
            validated_tree_sha=tree_sha,
        )
    assert ready.value.error_code == "code_task_pull_request_not_draft"

    _fake_github(
        monkeypatch,
        bare=_bare,
        existing=[
            {
                "number": 9,
                "html_url": "https://github.com/acme/project/pull/9",
                "draft": True,
                "state": "closed",
                "head": {"sha": "__REMOTE__"},
            }
        ],
    )
    with pytest.raises(ToolHandlerError) as closed:
        delivery._ensure_draft_pr(
            config=delivery.load_delivery_config(),
            job_dir=job_dir,
            state=json.loads(
                (job_dir / delivery.DELIVERY_FILENAME).read_text(encoding="utf-8")
            ),
            title="拒绝已关闭草稿",
            changed_files=["src/app.py"],
            checks=["quick", "full"],
        )
    assert closed.value.error_code == "code_task_pull_request_not_draft"



@pytest.mark.parametrize("created", [False, True])
def test_delivery_rejects_pr_not_bound_to_validated_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created: bool,
) -> None:
    _source, bare, job_dir, _calls = _prepare(tmp_path, monkeypatch)
    worktree = job_dir / "worktree"
    (worktree / "src" / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    tree_sha = delivery.compute_delivery_tree(
        job_dir=job_dir,
        worktree=worktree,
        changed_files=["src/app.py"],
        stage="validating",
    )
    if created:
        _fake_github(monkeypatch, bare=bare, created_head="")
    else:
        _fake_github(
            monkeypatch,
            bare=bare,
            existing=[
                {
                    "number": 10,
                    "html_url": "https://github.com/acme/project/pull/10",
                    "draft": True,
                    "state": "open",
                    "head": {"sha": "f" * 40},
                }
            ],
        )

    with pytest.raises(ToolHandlerError) as failed:
        delivery.deliver_pull_request(
            job_dir=job_dir,
            worktree=worktree,
            title="拒绝错误提交绑定",
            changed_files=["src/app.py"],
            checks=["quick", "full"],
            validated_tree_sha=tree_sha,
        )

    assert failed.value.error_code == "code_task_pull_request_conflict"


def test_delivery_recovers_pr_from_remote_branch_after_clone_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, bare, job_dir, _calls = _prepare(tmp_path, monkeypatch)
    worktree = job_dir / "worktree"
    (worktree / "src" / "app.py").write_text("VALUE = 6\n", encoding="utf-8")
    tree_sha = delivery.compute_delivery_tree(
        job_dir=job_dir,
        worktree=worktree,
        changed_files=["src/app.py"],
        stage="validating",
    )

    def fail_pr_creation(
        _config: delivery.GitHubDeliveryConfig,
        method: str,
        path: str,
        **_kwargs: Any,
    ) -> Any:
        if path.endswith("/pulls") and method == "GET":
            return []
        if path.endswith("/pulls") and method == "POST":
            raise ToolHandlerError(
                "simulated PR outage",
                error_code="code_task_github_unavailable",
                stage="delivering",
            )
        return {"default_branch": "main"}

    monkeypatch.setattr(delivery, "_github_request", fail_pr_creation)
    with pytest.raises(ToolHandlerError):
        delivery.deliver_pull_request(
            job_dir=job_dir,
            worktree=worktree,
            title="恢复远程分支交付",
            changed_files=["src/app.py"],
            checks=["quick", "full"],
            validated_tree_sha=tree_sha,
        )
    state = json.loads(
        (job_dir / delivery.DELIVERY_FILENAME).read_text(encoding="utf-8")
    )
    assert state["commit_sha"]
    assert _git(bare, "rev-parse", f"refs/heads/{state['branch']}") == state["commit_sha"]

    shutil.rmtree(worktree)
    _fake_github(monkeypatch, bare=bare)
    recovered = delivery.deliver_pull_request(
        job_dir=job_dir,
        worktree=worktree,
        title="恢复远程分支交付",
        changed_files=["src/app.py"],
        checks=["quick", "full"],
        validated_tree_sha=tree_sha,
    )

    assert recovered["delivered"] is True
    assert recovered["commit_sha"] == state["commit_sha"]
    assert recovered["pr_url"]

def test_git_error_redacts_token_and_machine_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bare = _repository(tmp_path)
    token_file = _configure(monkeypatch, tmp_path, source, bare)
    config = delivery.load_delivery_config()
    job_dir = tmp_path / "private-job"
    job_dir.mkdir()
    missing = tmp_path / "private-remote.git"
    worktree = job_dir / "worktree"

    with pytest.raises(ToolHandlerError) as failed:
        delivery._run_git(
            ["clone", missing.as_uri(), str(worktree)],
            cwd=job_dir,
            env=delivery._local_git_environment(job_dir),
            config=config,
            stage="preparing",
        )

    message = str(failed.value)
    assert str(tmp_path) not in message
    assert str(token_file) not in message
    assert "github_pat_unit_test_token" not in message
