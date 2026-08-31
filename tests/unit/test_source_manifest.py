from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from chatcopilot.core.source_manifest import (
    DEPLOYED_MANIFEST_FILENAME,
    is_deployable_source_path,
    read_manifest,
    reconcile_deployed_manifest,
    write_manifest,
)


@pytest.mark.parametrize(
    "path",
    [
        ".venv/bin/python",
        ".mypy_cache/cache.json",
        "build/package.whl",
        "dist/package.tar.gz",
        "console/web/dist/index.html",
        "package.egg-info/PKG-INFO",
        "reports/evals/report.json",
        "scratch_probe/result.txt",
        "tmp/scratch_probe/result.txt",
        "owner/jobs/job_1/worktree/file.py",
        "bots/demo/local.env",
        "deploy/wsl/secrets/feishu_app.json",
    ],
)
def test_central_manifest_excludes_build_eval_task_and_secret_paths(path: str) -> None:
    assert is_deployable_source_path(path) is False


def test_manifest_reconciliation_deletes_only_previous_deployable_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "runtime"
    source.mkdir()
    destination.mkdir()
    (source / "keep.py").write_text("new\n", encoding="utf-8")
    (destination / "keep.py").write_text("old\n", encoding="utf-8")
    (destination / "stale.py").write_text("stale\n", encoding="utf-8")
    (destination / ".venv").mkdir()
    (destination / ".venv" / "python").write_text("runtime\n", encoding="utf-8")
    (destination / DEPLOYED_MANIFEST_FILENAME).write_text(
        "keep.py\nstale.py\n.venv/python\n",
        encoding="utf-8",
    )

    stale = reconcile_deployed_manifest(
        source_root=source,
        destination_root=destination,
        current_paths=["keep.py"],
    )

    assert stale == ("stale.py",)
    assert not (destination / "stale.py").exists()
    assert (destination / ".venv" / "python").is_file()
    assert read_manifest(destination / DEPLOYED_MANIFEST_FILENAME) == {"keep.py"}


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_full_sync_uses_git_manifest_and_removes_previous_stale_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "runtime"
    (source / "src" / "chatcopilot").mkdir(parents=True)
    (source / "deploy" / "wsl").mkdir(parents=True)
    destination.mkdir()
    (source / "src" / "chatcopilot" / "app.py").write_text("new\n", encoding="utf-8")
    (source / "deploy" / "wsl" / "placeholder.sh").write_text(
        "#!/usr/bin/env bash\n",
        encoding="utf-8",
    )
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (source / "build").mkdir()
    (source / "build" / "garbage.bin").write_bytes(b"garbage")
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    (destination / "stale.py").write_text("stale\n", encoding="utf-8")
    (destination / ".venv").mkdir()
    (destination / ".venv" / "python").write_text("runtime\n", encoding="utf-8")
    write_manifest(
        destination / DEPLOYED_MANIFEST_FILENAME,
        ["stale.py"],
    )
    script = Path(__file__).resolve().parents[2] / "deploy" / "wsl" / "sync_code.sh"

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--src",
            str(source),
            "--dst",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "AGENTSTRATA_DEPLOY_PYTHON": sys.executable,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (destination / "src" / "chatcopilot" / "app.py").read_text(
        encoding="utf-8"
    ) == "new\n"
    assert not (destination / "build").exists()
    assert not (destination / "stale.py").exists()
    assert (destination / ".venv" / "python").is_file()


def test_status_requires_explicit_instance_from_control_checkout() -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "deploy" / "wsl" / "status.sh"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CHATCOPILOT_")
    }

    result = subprocess.run(
        ["bash", str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 2
    assert "--instance" in result.stderr


def test_status_resolves_explicit_botspec_without_defaulting_platform(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "deploy" / "wsl" / "status.sh"
    bot = tmp_path / "bot.yaml"
    bot.write_text(
        "id: status-fixture\n"
        "display_name: Status Fixture\n"
        "gateway:\n"
        "  protocol_version: 1\n"
        "  host: 127.0.0.1\n"
        "  port_env: CHATCOPILOT_GATEWAY_PORT\n"
        "  token_env: CHATCOPILOT_GATEWAY_TOKEN\n"
        "  state_root_env: CHATCOPILOT_GATEWAY_STATE_ROOT\n"
        "channels:\n"
        "  qq:\n"
        "    type: qq_personal\n"
        "    provider: onebot_v11\n"
        "    channel_id: qq\n"
        "    endpoint_env: CHATCOPILOT_QQ_ONEBOT_WS_URL\n"
        "    access_token_env: QQ_ACCESS_TOKEN\n"
        "    account_env: QQ_ACCOUNT\n"
        "    mention_only_groups: true\n"
        "workspace:\n"
        "  root_env: CHATCOPILOT_WORKSPACE_ROOT\n"
        "deploy:\n"
        "  target: wsl2\n"
        "  instance_id: status-fixture\n"
        f"  wsl_home: {tmp_path / 'runtime'}\n"
        f"  workspace_root: {tmp_path / 'workspace'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CHATCOPILOT_")
    }
    env["AGENTSTRATA_DEPLOY_PYTHON"] = sys.executable

    result = subprocess.run(
        ["bash", str(script), "--bot-spec", str(bot)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "instance=status-fixture" in result.stdout
    assert "AgentStrata Gateway 状态" in result.stdout
    assert "cc-connect 健康检查" not in result.stdout
