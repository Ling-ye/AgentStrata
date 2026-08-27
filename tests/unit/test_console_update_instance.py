from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from ruamel.yaml import YAML

from console.backend.app import app
from console.backend.tasks import TaskManager
from console.control import operations
from console.control.instances import BotInstance


_REBUILD_INPUTS = (
    "pyproject.toml",
    "uv.lock",
    "deploy/wsl/install_wsl_env.sh",
    "deploy/wsl/node-tools/package.json",
    "deploy/wsl/node-tools/package-lock.json",
    "deploy/wsl/bootstrap_wsl.sh",
)


def _inst(tmp_path: Path) -> BotInstance:
    return BotInstance(
        instance_id="sample-bot",
        bot_spec="bots/sample-bot/bot.yaml",
        display_name="SampleBot",
        platform="feishu",
        wsl_home=str(tmp_path / "ChatCopilot-sample-bot"),
        workspace_root=str(tmp_path / "workspace"),
        log_dir=str(tmp_path / "logs"),
        env_file=str(tmp_path / ".chatcopilot-sample-bot.env"),
        cc_connect_config_dir=str(tmp_path / ".runtime" / ".cc-connect"),
        cc_home=str(tmp_path / ".runtime"),
        project_name="chatcopilot-sample-bot",
    )


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _make_update_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    stage_log = tmp_path / "stages.log"
    fake_bin = tmp_path / "bin"

    for rel in _REBUILD_INPUTS:
        content = f"{rel}\n"
        if rel == "deploy/wsl/bootstrap_wsl.sh":
            content = (
                "#!/usr/bin/env bash\n"
                'echo bootstrap >> "$STAGE_LOG"\n'
                'if [ "${FAIL_STAGE:-}" = "rebuild" ]; then\n'
                '  echo "[ERR] bootstrap detail" >&2\n'
                "  exit 24\n"
                "fi\n"
            )
        elif rel == "deploy/wsl/install_wsl_env.sh":
            content = (
                "#!/usr/bin/env bash\n"
                'if [ "$PWD" = "$RUNTIME_ROOT_FIXTURE" ]; then\n'
                '  echo runtime-deps >> "$STAGE_LOG"\n'
                '  if [ "${FAIL_STAGE:-}" = "runtime-deps" ]; then\n'
                '    echo "[ERR] locked runtime sync detail" >&2\n'
                "    exit 26\n"
                "  fi\n"
                "else\n"
                '  echo source-deps >> "$STAGE_LOG"\n'
                "fi\n"
                'if [ "${FAIL_STAGE:-}" = "source-deps" ]; then\n'
                '  echo "[ERR] locked source sync detail" >&2\n'
                "  exit 20\n"
                "fi\n"
                'if [ "${FAIL_STAGE:-}" = "bot-drift" ]; then\n'
                '  printf "id: test-bot\\nmarker: drifted-during-deps\\n" > "$SOURCE_BOT"\n'
                "fi\n"
            )
        for root in (source, runtime):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    (source / "src/chatcopilot").mkdir(parents=True, exist_ok=True)
    for root in (source, runtime):
        bot_dir = root / "bots/test-bot"
        bot_dir.mkdir(parents=True)
        (bot_dir / "bot.yaml").write_text("id: test-bot\n", encoding="utf-8")
    (source / "bots/test-bot/local.env").write_text(
        "CHATCOPILOT_CHAT_API_KEY=test\n",
        encoding="utf-8",
    )

    fake_python = (
        "#!/usr/bin/env bash\n"
        'if [ -n "${CHATCOPILOT_REQUIREMENT_BOT:-}" ]; then\n'
        '  if grep -Fq -- "- dev.code_tasks" "$CHATCOPILOT_REQUIREMENT_BOT"; then echo 1; else echo 0; fi\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$*" == *"provision-env"* ]]; then\n'
        '  echo provision >> "$STAGE_LOG"\n'
        '  if [ "${FAIL_STAGE:-}" = "provision" ]; then\n'
        '    echo "[ERR] provision detail" >&2\n'
        "    exit 21\n"
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    _write_executable(source / ".venv/bin/python", fake_python)
    _write_executable(runtime / ".venv/bin/python", fake_python)
    _write_executable(
        source / "deploy/wsl/sync_code.sh",
        "#!/usr/bin/env bash\n"
        'echo sync >> "$STAGE_LOG"\n'
        'if [ "${FAIL_STAGE:-}" = "sync" ]; then\n'
        '  echo "[ERR] sync detail" >&2\n'
        "  exit 22\n"
        "fi\n"
        'sync_src=""\n'
        'sync_dst=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --src) sync_src="$2"; shift 2 ;;\n'
        '    --dst) sync_dst="$2"; shift 2 ;;\n'
        '    *) shift ;;\n'
        "  esac\n"
        "done\n"
        'if [ -f "$sync_src/bots/test-bot/bot.yaml" ]; then\n'
        '  mkdir -p "$sync_dst/bots/test-bot"\n'
        '  cp "$sync_src/bots/test-bot/bot.yaml" "$sync_dst/bots/test-bot/bot.yaml"\n'
        "fi\n"
        'if [ -f "$sync_src/uv.lock" ]; then\n'
        '  cp "$sync_src/uv.lock" "$sync_dst/uv.lock"\n'
        "fi\n",
    )
    _write_executable(
        runtime / "deploy/wsl/_apply_config.sh",
        "#!/usr/bin/env bash\n"
        'echo apply >> "$STAGE_LOG"\n'
        'if [ "${FAIL_STAGE:-}" = "apply" ]; then\n'
        '  echo "[ERR] apply detail" >&2\n'
        "  exit 23\n"
        "fi\n",
    )
    _write_executable(
        source / "console/scripts/ctl.sh",
        "#!/usr/bin/env bash\n"
        'echo restart >> "$STAGE_LOG"\n'
        'if [ "${FAIL_STAGE:-}" = "restart" ]; then\n'
        '  echo "[ERR] restart detail" >&2\n'
        "  exit 25\n"
        "fi\n",
    )
    _write_executable(
        source / "console/systemd/register.sh",
        "#!/usr/bin/env bash\n"
        'echo register >> "$STAGE_LOG"\n'
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        'if [ -n "${SYSTEMCTL_LOG:-}" ]; then echo "$*" >> "$SYSTEMCTL_LOG"; fi\n'
        'if [[ "$*" == *" cat chatcopilot-code-worker@"* ]] && [ "${WORKER_UNIT_EXISTS:-0}" != 1 ]; then exit 1; fi\n'
        'if [[ "$*" == *" is-active "*"chatcopilot@"* ]]; then\n'
        '  [ "${FAIL_STAGE:-}" != "inactive" ] || exit 1\n'
        "fi\n"
        "exit 0\n",
    )
    return source, runtime, stage_log, fake_bin


def _run_update_fixture(
    source: Path,
    runtime: Path,
    stage_log: Path,
    fake_bin: Path,
    *,
    fail_stage: str = "",
    dry_run: bool = False,
    sync_source: Path | None = None,
    changed_files: tuple[str, ...] = ("bots/test-bot/bot.yaml",),
    destination: str | None = None,
    home: Path | None = None,
    enable_service: bool = False,
    worker_unit_exists: bool = False,
    systemctl_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    script = Path("deploy/wsl/update_instance.sh").resolve()
    args = [
        "bash",
        str(script),
        "--instance",
        "test-bot",
        "--src",
        str(source),
        "--dst",
        destination or str(runtime),
        "--bot",
        "bots/test-bot/bot.yaml",
    ]
    if sync_source is not None:
        manifest = source.parent / "changed-files.txt"
        manifest.write_text(
            "".join(f"{path}\n" for path in changed_files),
            encoding="utf-8",
        )
        args.extend(["--sync-src", str(sync_source), "--changed-files", str(manifest)])
    if dry_run:
        args.append("--dry-run")
    if enable_service:
        args.append("--enable")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "STAGE_LOG": str(stage_log),
        "RUNTIME_ROOT_FIXTURE": str(runtime),
        "FAIL_STAGE": fail_stage,
        "SOURCE_BOT": str(source / "bots/test-bot/bot.yaml"),
        "WORKER_UNIT_EXISTS": "1" if worker_unit_exists else "0",
    }
    if systemctl_log is not None:
        env["SYSTEMCTL_LOG"] = str(systemctl_log)
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=20)


def test_stream_update_calls_integrated_script(tmp_path: Path) -> None:
    repo = tmp_path / "ChatCopilot"
    script = repo / "deploy" / "wsl" / "update_instance.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    inst = _inst(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_streaming(args, *, cwd=None, extra_env=None):
        captured["args"] = args
        captured["cwd"] = cwd
        yield "[OK] fake update"
        yield "__EXIT__ 0"

    with (
        patch("console.control.operations.repo_root", return_value=repo),
        patch("console.control.operations.run_streaming", fake_run_streaming),
    ):
        lines = list(operations.stream_update(inst))

    assert lines[0].startswith("[console] 一键更新")
    assert lines[-1] == "__EXIT__ 0"
    args = captured["args"]
    assert args[:2] == ["bash", str(script)]
    assert "--instance" in args
    assert "sample-bot" in args
    assert "--dst" in args
    assert inst.wsl_home in args
    assert captured["cwd"] == str(repo)


def test_update_script_sets_pythonpath_for_provision_env() -> None:
    text = Path("deploy/wsl/update_instance.sh").read_text(encoding="utf-8")

    assert 'VENV_PY="$SRC/.venv/bin/python"' in text
    assert 'bash "$installer" --no-system-packages --skip-cc-connect' in text
    assert '--venv "$venv_dir" --no-verify' in text
    assert 'python3 -m venv "$venv_dir"' not in text
    assert ' -m pip install ' not in text
    assert 'export PYTHONPATH="$SRC/src${PYTHONPATH:+:$PYTHONPATH}"' in text
    assert '"$PY" -m chatcopilot bot provision-env --bot "$BOT_FOR_CMD"' in text
    assert 'PY="$(command -v python3 || command -v python || true)"' not in text
    assert "command -v python3" not in text


def test_update_script_dry_run_reports_selected_python_and_pythonpath() -> None:
    text = Path("deploy/wsl/update_instance.sh").read_text(encoding="utf-8")

    assert "[DRY-RUN] would ensure source venv from:" in text
    assert "[DRY-RUN] would run locked installer:" in text
    assert "[DRY-RUN] would reconcile source CLI with uv sync --frozen" in text
    assert '[DRY-RUN] would export: PYTHONPATH=' in text
    assert '[DRY-RUN] would run: \'$VENV_PY\' -m chatcopilot bot provision-env' in text
    assert "[DRY-RUN] selected update mode:" in text
    assert "[DRY-RUN] would run: python -m chatcopilot" not in text


def test_update_script_dry_run_never_executes_existing_source_python(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    marker = tmp_path / "untrusted-python-executed"
    _write_executable(
        source / ".venv/bin/python",
        "#!/usr/bin/env bash\n" f'touch "{marker}"\n' "exit 0\n",
    )

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        dry_run=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "deferred until locked source CLI reconciliation" in result.stdout
    assert not marker.exists()
    assert not stage_log.exists()


def test_update_script_reconciles_before_executing_existing_source_python(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    reconciled = tmp_path / "source-reconciled"
    unsafe_execution = tmp_path / "pre-reconcile-python-executed"
    installer = (
        "#!/usr/bin/env bash\n"
        'if [ "$PWD" = "$RUNTIME_ROOT_FIXTURE" ]; then\n'
        '  echo runtime-deps >> "$STAGE_LOG"\n'
        "else\n"
        '  echo source-deps >> "$STAGE_LOG"\n'
        f'  touch "{reconciled}"\n'
        "fi\n"
    )
    for root in (source, runtime):
        _write_executable(root / "deploy/wsl/install_wsl_env.sh", installer)
    guarded_python = (
        "#!/usr/bin/env bash\n"
        f'if [ ! -f "{reconciled}" ]; then touch "{unsafe_execution}"; fi\n'
        'if [ -n "${CHATCOPILOT_REQUIREMENT_BOT:-}" ]; then echo 0; exit 0; fi\n'
        'if [[ "$*" == *"provision-env"* ]]; then echo provision >> "$STAGE_LOG"; fi\n'
        "exit 0\n"
    )
    _write_executable(source / ".venv/bin/python", guarded_python)

    result = _run_update_fixture(source, runtime, stage_log, fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert reconciled.exists()
    assert not unsafe_execution.exists()
    assert stage_log.read_text(encoding="utf-8").splitlines() == [
        "source-deps",
        "provision",
        "sync",
        "runtime-deps",
        "apply",
        "register",
        "restart",
    ]


def test_update_script_expands_quoted_tilde_destination(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)

    completed = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        dry_run=True,
        destination="~/runtime",
        home=runtime.parent,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"[update] dst:      {runtime}" in completed.stdout


def test_update_script_enable_is_post_start_and_fail_closed() -> None:
    text = Path("deploy/wsl/update_instance.sh").read_text(encoding="utf-8")

    assert "--enable) ENABLE_SERVICE=1" in text
    restart = text.index('bash "$CTL" restart "$INSTANCE"')
    enable = text.index('systemctl --user enable "$UNIT"', restart)
    assert enable > restart
    assert 'echo "[ERR] systemctl not found"' in text
    assert 'systemctl --user is-active --quiet "$UNIT"' in text
    assert text.count("systemctl --user is-active --quiet") == 2
    assert 'systemctl --user is-active --quiet "$CODE_WORKER_UNIT"' in text
    assert 'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$user_uid}"' in text
    assert 'export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$user_uid/bus}"' in text
    assert 'fail_stage "register service" "$rc"' in text


def test_update_script_uses_fast_path_when_rebuild_inputs_match(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    updated_bot = "id: test-bot\nmarker: fast-update-applied\n"
    (source / "bots/test-bot/bot.yaml").write_text(updated_bot, encoding="utf-8")

    result = _run_update_fixture(source, runtime, stage_log, fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mode:     fast" in result.stdout
    assert (runtime / "bots/test-bot/bot.yaml").read_text(encoding="utf-8") == updated_bot
    assert stage_log.read_text(encoding="utf-8").splitlines() == [
        "source-deps",
        "provision",
        "sync",
        "runtime-deps",
        "apply",
        "register",
        "restart",
    ]


def test_update_script_starts_and_enables_worker_only_when_pack_is_enabled(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    bot = "id: test-bot\ntools:\n  packs:\n  - dev.code_tasks\n"
    (source / "bots/test-bot/bot.yaml").write_text(bot, encoding="utf-8")
    systemctl_log = tmp_path / "systemctl.log"

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        enable_service=True,
        worker_unit_exists=True,
        systemctl_log=systemctl_log,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    worker_unit = "chatcopilot-code-worker" + "@" + "test-bot.service"
    main_unit = "chatcopilot" + "@" + "test-bot.service"
    assert f"--user restart {worker_unit}" in calls
    assert f"--user is-active --quiet {worker_unit}" in calls
    assert f"--user enable {main_unit}" in calls
    assert f"--user enable {worker_unit}" in calls


def test_update_script_skips_worker_for_bot_without_code_task_pack(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    systemctl_log = tmp_path / "systemctl.log"

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        enable_service=True,
        worker_unit_exists=True,
        systemctl_log=systemctl_log,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    main_unit = "chatcopilot" + "@" + "test-bot.service"
    worker_prefix = "chatcopilot-code-worker" + "@"
    assert f"--user enable {main_unit}" in calls
    assert not any(worker_prefix in call for call in calls)
    assert "code worker not applicable" in result.stdout


@pytest.mark.parametrize("changed_rel", _REBUILD_INPUTS)
def test_update_script_rebuilds_when_input_changes(
    tmp_path: Path,
    changed_rel: str,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    changed_path = source / changed_rel
    if changed_rel == "deploy/wsl/install_wsl_env.sh":
        changed_path.write_text(
            changed_path.read_text(encoding="utf-8") + "# changed\n",
            encoding="utf-8",
        )
    else:
        changed_path.write_text("changed\n", encoding="utf-8")

    result = _run_update_fixture(source, runtime, stage_log, fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mode:     full" in result.stdout
    assert f"{changed_rel} changed" in result.stdout
    stages = stage_log.read_text(encoding="utf-8").splitlines()
    assert stages.index("source-deps") < stages.index("provision")
    assert "bootstrap" in stages
    assert "apply" not in stages


def test_update_script_rebuilds_when_runtime_venv_is_missing(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    (runtime / ".venv/bin/python").unlink()

    result = _run_update_fixture(source, runtime, stage_log, fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mode:     full (runtime venv is missing)" in result.stdout
    stages = stage_log.read_text(encoding="utf-8").splitlines()
    assert stages.index("source-deps") < stages.index("provision")
    assert "bootstrap" in stages


def test_changed_files_mode_compares_canonical_source_inputs(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    (overlay / "bots/test-bot").mkdir(parents=True)
    (overlay / "bots/test-bot/bot.yaml").write_text("id: test-bot\n", encoding="utf-8")
    (source / "uv.lock").write_text("unrelated source drift\n", encoding="utf-8")

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        sync_source=overlay,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mode:     fast" in result.stdout
    assert stage_log.read_text(encoding="utf-8").splitlines() == [
        "source-deps",
        "provision",
        "sync",
        "runtime-deps",
        "apply",
        "register",
        "restart",
    ]


def test_changed_files_mode_rebuilds_for_selected_overlay_input(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "uv.lock").write_text("changed\n", encoding="utf-8")
    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        sync_source=overlay,
        changed_files=("uv.lock",),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mode:     full (uv.lock changed)" in result.stdout
    assert (runtime / "uv.lock").read_text(encoding="utf-8") == "changed\n"
    stages = stage_log.read_text(encoding="utf-8").splitlines()
    assert stages.index("source-deps") < stages.index("provision")
    assert "bootstrap" in stages


def test_changed_files_mode_rejects_unselected_missing_runtime_input(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    (overlay / "bots/test-bot").mkdir(parents=True)
    (overlay / "bots/test-bot/bot.yaml").write_text("id: test-bot\n", encoding="utf-8")
    (runtime / "uv.lock").unlink()

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        sync_source=overlay,
    )

    assert result.returncode == 1
    assert (
        "uv.lock is missing from runtime but absent from changed-files manifest"
        in result.stderr
    )
    assert "sync code to instance failed (exit 1)" in result.stderr
    assert not stage_log.exists()


def test_changed_files_mode_rejects_bot_snapshot_drift(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    (overlay / "bots/test-bot").mkdir(parents=True)
    (overlay / "bots/test-bot/bot.yaml").write_text(
        "id: test-bot\nmarker: frozen\n",
        encoding="utf-8",
    )

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        sync_source=overlay,
    )

    assert result.returncode == 1
    assert "BotSpec used for provision differs from the changed-files deployment target" in result.stderr
    assert "provision runtime env failed (exit 1)" in result.stderr
    assert not stage_log.exists()


def test_changed_files_mode_rejects_unselected_bot_source_drift(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "uv.lock").write_text(
        (runtime / "uv.lock").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (source / "bots/test-bot/bot.yaml").write_text(
        "id: test-bot\nmarker: unselected-source-drift\n",
        encoding="utf-8",
    )

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        sync_source=overlay,
        changed_files=("uv.lock",),
    )

    assert result.returncode == 1
    assert "BotSpec used for provision differs from the changed-files deployment target" in result.stderr
    assert "provision runtime env failed (exit 1)" in result.stderr
    assert not stage_log.exists()


@pytest.mark.parametrize("runtime_venv_missing", (False, True))
def test_changed_files_mode_rejects_selected_missing_update_input(
    tmp_path: Path,
    runtime_venv_missing: bool,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (runtime / "uv.lock").unlink()
    if runtime_venv_missing:
        (runtime / ".venv/bin/python").unlink()

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        sync_source=overlay,
        changed_files=("uv.lock",),
    )

    assert result.returncode == 1
    assert "uv.lock is selected but missing from update source" in result.stderr
    assert "sync code to instance failed (exit 1)" in result.stderr
    assert not stage_log.exists()


def test_changed_files_mode_rechecks_bot_after_source_dependency_refresh(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "uv.lock").write_text("changed\n", encoding="utf-8")

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        fail_stage="bot-drift",
        sync_source=overlay,
        changed_files=("uv.lock",),
    )

    assert result.returncode == 1
    assert "BotSpec used for provision differs from the changed-files deployment target" in result.stderr
    assert "provision runtime env failed (exit 1)" in result.stderr
    assert stage_log.read_text(encoding="utf-8").splitlines() == ["source-deps"]


@pytest.mark.parametrize(
    ("fail_stage", "expected_rc", "expected_error", "original_error", "forbidden_stage"),
    (
        ("provision", 21, "provision runtime env failed (exit 21)", "provision detail", "sync"),
        ("sync", 22, "sync code to instance failed (exit 22)", "sync detail", "apply"),
        (
            "apply",
            23,
            "reconcile runtime and render config failed (exit 23)",
            "apply detail",
            "register",
        ),
        ("restart", 25, "restart service failed (exit 25)", "restart detail", None),
        ("inactive", 1, "restart service failed (exit 1)", "service is not active after restart", None),
    ),
)
def test_update_script_reports_stage_failures(
    tmp_path: Path,
    fail_stage: str,
    expected_rc: int,
    expected_error: str,
    original_error: str,
    forbidden_stage: str | None,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        fail_stage=fail_stage,
    )

    assert result.returncode == expected_rc
    assert expected_error in result.stderr
    assert original_error in result.stderr
    if forbidden_stage:
        assert forbidden_stage not in stage_log.read_text(encoding="utf-8").splitlines()


def test_update_script_reports_rebuild_failure(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    (source / "uv.lock").write_text("changed\n", encoding="utf-8")

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        fail_stage="rebuild",
    )

    assert result.returncode == 24
    assert "bootstrap detail" in result.stderr
    assert "rebuild environment failed (exit 24)" in result.stderr
    assert "restart" not in stage_log.read_text(encoding="utf-8").splitlines()


def test_failed_full_rebuild_is_reconciled_on_the_next_fast_resume(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    (source / "uv.lock").write_text("changed\n", encoding="utf-8")

    failed = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        fail_stage="rebuild",
    )

    assert failed.returncode == 24
    assert (runtime / "uv.lock").read_text(encoding="utf-8") == "changed\n"
    stage_log.unlink()

    resumed = _run_update_fixture(source, runtime, stage_log, fake_bin)

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "mode:     fast" in resumed.stdout
    assert stage_log.read_text(encoding="utf-8").splitlines() == [
        "source-deps",
        "provision",
        "sync",
        "runtime-deps",
        "apply",
        "register",
        "restart",
    ]


def test_fast_update_stops_when_locked_runtime_reconciliation_fails(
    tmp_path: Path,
) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        fail_stage="runtime-deps",
    )

    assert result.returncode == 26
    assert "locked runtime sync detail" in result.stderr
    assert "reconcile runtime and render config failed (exit 26)" in result.stderr
    assert stage_log.read_text(encoding="utf-8").splitlines() == [
        "source-deps",
        "provision",
        "sync",
        "runtime-deps",
    ]


def test_full_update_reports_source_dependency_refresh_failure(tmp_path: Path) -> None:
    source, runtime, stage_log, fake_bin = _make_update_fixture(tmp_path)
    (source / "uv.lock").write_text("changed\n", encoding="utf-8")

    result = _run_update_fixture(
        source,
        runtime,
        stage_log,
        fake_bin,
        fail_stage="source-deps",
    )

    assert result.returncode == 20
    assert "locked source sync detail" in result.stderr
    assert "provision runtime env failed (exit 20)" in result.stderr
    assert stage_log.read_text(encoding="utf-8").splitlines() == ["source-deps"]


def test_register_script_fails_when_daemon_reload_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir()
    credential_dir = home / ".config" / "chatcopilot-console"
    credential_dir.mkdir(parents=True, mode=0o700)
    credential_dir.chmod(0o700)
    token_file = credential_dir / "lingye-copilot-qq-code-worker-github.token"
    token_file.write_text("fixture-" + "x" * 32 + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"daemon-reload"* ]]; then\n'
        '  echo "daemon reload detail" >&2\n'
        "  exit 17\n"
        "fi\n"
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "loginctl",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "show-user" ]; then echo "Linger=yes"; fi\n'
        "exit 0\n",
    )

    result = subprocess.run(
        ["bash", "console/systemd/register.sh", "lingye-copilot-qq"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "AGENTSTRATA_DEPLOY_PYTHON": sys.executable,
        },
        timeout=20,
    )

    assert result.returncode != 0
    assert "daemon reload detail" in result.stderr
    assert "systemctl --user daemon-reload 失败" in result.stderr
    assert "systemd --user 已 reload" not in result.stdout


def test_register_script_fails_when_unit_copy_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir()
    _write_executable(
        fake_bin / "cp",
        "#!/usr/bin/env bash\n"
        'echo "unit copy detail" >&2\n'
        "exit 17\n",
    )

    result = subprocess.run(
        ["bash", "console/systemd/register.sh", "lingye-copilot-qq"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "AGENTSTRATA_DEPLOY_PYTHON": sys.executable,
        },
        timeout=20,
    )

    assert result.returncode != 0
    assert "unit copy detail" in result.stderr
    assert "安装模板 unit 失败" in result.stderr
    assert "systemd --user 已 reload" not in result.stdout


def test_register_script_fails_when_worker_env_generation_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir()
    _write_executable(
        fake_bin / "python3",
        "#!/usr/bin/env bash\n"
        'if [ -n "${CHATCOPILOT_REGISTER_WORKER_ENV:-}" ]; then\n'
        '  echo "worker env detail" >&2\n'
        "  exit 19\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
    )
    _write_executable(
        fake_bin / "loginctl",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "show-user" ]; then echo "Linger=yes"; fi\n'
        "exit 0\n",
    )

    result = subprocess.run(
        ["bash", "console/systemd/register.sh", "lingye-copilot-qq"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "AGENTSTRATA_DEPLOY_PYTHON": str(fake_bin / "python3"),
        },
        timeout=20,
    )

    assert result.returncode != 0
    assert "worker env detail" in result.stderr
    assert "生成代码任务 worker 环境失败" in result.stderr
    assert "systemd --user 已 reload" not in result.stdout


def test_update_api_creates_task(tmp_path: Path) -> None:
    inst = _inst(tmp_path)

    with (
        patch("console.backend.routes.bots.get_instance", return_value=inst),
        patch("console.control.operations.stream_update", lambda _inst, dry_run=False: iter(["ok", "__EXIT__ 0"])),
    ):
        response = TestClient(app).post("/api/bots/sample-bot/update")

    assert response.status_code == 200
    body = response.json()
    assert body["instance_id"] == "sample-bot"
    assert body["kind"] == "update"


def test_apply_tools_api_creates_unified_update_task(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    repo = tmp_path / "ChatCopilot"
    bot_yaml = repo / "bots" / "sample-bot" / "bot.yaml"
    bot_yaml.parent.mkdir(parents=True)
    bot_yaml.write_text(
        "id: sample-bot\ntools:\n  packs:\n    - old.pack\n",
        encoding="utf-8",
    )

    previous_manager = app.state.tasks
    app.state.tasks = TaskManager()
    try:
        with (
            patch("console.backend.routes.bots.get_instance", return_value=inst),
            patch("console.backend.routes.bots.repo_root", return_value=repo),
            patch("console.control.operations.stream_update", lambda _inst: iter(["updated", "__EXIT__ 0"])),
        ):
            response = TestClient(app).put(
                "/api/bots/sample-bot/tools?apply=true",
                json={
                    "tools": {
                        "packs": ["workspace.read_write"],
                        "features": [],
                        "hide": [],
                        "mcp": {"servers": []},
                    },
                    "agents": {"presets": [], "workflows": []},
                },
            )

            assert response.status_code == 200
            body = response.json()
            assert body["instance_id"] == "sample-bot"
            assert body["kind"] == "apply-tools"
            task = app.state.tasks.get(body["id"])
            assert task is not None
            list(task.follow(timeout=1))
            assert task.status == "done", task.lines
        data = YAML(typ="safe").load(bot_yaml.read_text(encoding="utf-8"))
        assert data["tools"]["packs"] == ["workspace.read_write"]
    finally:
        app.state.tasks = previous_manager


def test_apply_tools_conflict_does_not_mutate_config(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    repo = tmp_path / "ChatCopilot"
    bot_yaml = repo / "bots" / "sample-bot" / "bot.yaml"
    bot_yaml.parent.mkdir(parents=True)
    original = "id: sample-bot\ntools:\n  packs:\n    - old.pack\n"
    bot_yaml.write_text(original, encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()

    def blocking_task():
        entered.set()
        release.wait(timeout=5)
        yield "__EXIT__ 0"

    previous_manager = app.state.tasks
    manager = TaskManager()
    app.state.tasks = manager
    active = manager.start(inst.instance_id, "existing", blocking_task)
    assert entered.wait(timeout=2)
    try:
        with (
            patch("console.backend.routes.bots.get_instance", return_value=inst),
            patch("console.backend.routes.bots.repo_root", return_value=repo),
        ):
            response = TestClient(app).put(
                "/api/bots/sample-bot/tools?apply=true",
                json={
                    "tools": {
                        "packs": ["new.pack"],
                        "features": [],
                        "hide": [],
                        "mcp": {"servers": []},
                    },
                    "agents": {"presets": [], "workflows": []},
                },
            )

        assert response.status_code == 409
        assert bot_yaml.read_text(encoding="utf-8") == original
    finally:
        release.set()
        list(active.follow(timeout=1))
        app.state.tasks = previous_manager


def test_tools_api_clears_existing_mcp_servers(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    repo = tmp_path / "ChatCopilot"
    bot_yaml = repo / "bots" / "sample-bot" / "bot.yaml"
    servers_yaml = bot_yaml.parent / "mcp" / "servers.yaml"
    servers_yaml.parent.mkdir(parents=True)
    bot_yaml.write_text(
        "id: sample-bot\n"
        "tools:\n"
        "  packs:\n"
        "    - workspace.read_write\n"
        "  mcp:\n"
        "    servers: mcp/servers.yaml\n"
        "agents:\n"
        "  presets: []\n",
        encoding="utf-8",
    )
    servers_yaml.write_text("servers:\n  - ref: searxng-search\n    enabled: true\n", encoding="utf-8")

    with (
        patch("console.backend.routes.bots.get_instance", return_value=inst),
        patch("console.backend.routes.bots.repo_root", return_value=repo),
    ):
        response = TestClient(app).put(
            "/api/bots/sample-bot/tools",
            json={
                "tools": {
                    "packs": ["workspace.read_write"],
                    "features": [],
                    "hide": [],
                    "mcp": {"servers": []},
                },
                "agents": {"presets": [], "workflows": []},
            },
        )

    assert response.status_code == 200
    data = YAML(typ="safe").load(servers_yaml.read_text(encoding="utf-8"))
    assert data == {"servers": []}


def test_bot_workspace_exposes_single_update_button() -> None:
    text = Path("console/web/src/pages/BotsPage.tsx").read_text(encoding="utf-8")

    assert text.count('handleAction(selectedBot, "update")') == 1
    assert 'handleAction(selectedBot, "sync")' not in text
    assert 'handleAction(selectedBot, "rebuild")' not in text
    assert text.count("更新并重启") == 1
    assert "更新代码并重启" not in text
    assert "<BotToolEditor" in text
    assert "onApplyTask=" in text


def test_bot_tool_editor_applies_config_via_unified_update() -> None:
    component = Path("console/web/src/components/BotToolEditor.tsx").read_text(
        encoding="utf-8"
    )
    hook = Path(
        "console/web/src/features/bots/tool-editor/useBotToolEditor.ts"
    ).read_text(encoding="utf-8")

    assert "apply: apply && isDeployed" in hook
    assert "保存并重启" in component
    assert "onRestart" not in component + hook
    task_branch = hook.split('if ("id" in result)', 1)[1].split("return;", 1)[0]
    assert "onApplyTask?.(result, () => {" in task_branch
    assert task_branch.index("onApplyTask?.") < task_branch.index("setDirty(false)")
    assert "void refetch()" in task_branch


def test_bot_update_task_resolves_terminal_state_in_drawer() -> None:
    page = Path("console/web/src/pages/BotsPage.tsx").read_text(encoding="utf-8")
    sheet = Path("console/web/src/shared/ui/TaskStreamSheet.tsx").read_text(
        encoding="utf-8"
    )

    assert "api.task(task.id)" in page
    assert 'finished.status === "done"' in page
    assert "options?.onSuccess?.()" in page
    assert "activeTaskId.current = null" in page
    assert "onClose={closeTask}" in page
    assert "taskStream.close()" in page
    assert 'finished.status === "failed"' in page
    assert "setTaskError(detail)" in page
    assert 'color="red">失败' in sheet
    assert '<Alert type="error"' in sheet
