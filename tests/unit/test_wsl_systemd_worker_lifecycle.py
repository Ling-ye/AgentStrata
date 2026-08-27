from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _registration_fixture(
    tmp_path: Path,
    *,
    requires_code_worker: bool,
) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repository"
    systemd_dir = repository / "console" / "systemd"
    systemd_dir.mkdir(parents=True)
    for name in (
        "register.sh",
        "chatcopilot@.service",
        "chatcopilot-code-worker@.service",
    ):
        shutil.copy2(REPO_ROOT / "console" / "systemd" / name, systemd_dir / name)

    packs = "  - dev.code_tasks\n" if requires_code_worker else "  - memory.chat\n"
    bot_dir = repository / "bots" / "starter"
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.yaml").write_text(
        "id: starter\n"
        "tools:\n"
        "  packs:\n"
        f"{packs}"
        "deploy:\n"
        "  instance_id: starter\n"
        "  wsl_home: ~/AgentStrata-starter\n",
        encoding="utf-8",
    )
    local_env = bot_dir / "local.env"
    local_env.write_text("", encoding="utf-8")
    local_env.chmod(0o600)

    home = tmp_path / "home"
    config_dir = home / ".config" / "chatcopilot-console"
    config_dir.mkdir(parents=True, mode=0o700)
    config_dir.chmod(0o700)
    stale_worker_env = config_dir / "starter-code-worker.env"
    stale_worker_env.write_text("PRESERVED=1\n", encoding="utf-8")
    stale_worker_env.chmod(0o600)

    fake_bin = tmp_path / "bin"
    systemctl_log = tmp_path / "systemctl.log"
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n'
        'if [[ "$*" == *"enable chatcopilot@starter"* ]] '
        '&& [ "${FAIL_MAIN_ENABLE:-0}" = 1 ]; then exit 23; fi\n'
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "loginctl",
        '#!/usr/bin/env bash\nif [ "${1:-}" = "show-user" ]; then echo "Linger=yes"; fi\nexit 0\n',
    )
    return repository, home, fake_bin, systemctl_log


def _run_register(
    repository: Path,
    home: Path,
    fake_bin: Path,
    systemctl_log: Path,
    *,
    fail_main_enable: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(repository / "console" / "systemd" / "register.sh"),
            "--enable",
            "starter",
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "SYSTEMCTL_LOG": str(systemctl_log),
            "FAIL_MAIN_ENABLE": "1" if fail_main_enable else "0",
            "AGENTSTRATA_DEPLOY_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_registration_disables_existing_worker_when_pack_is_absent(
    tmp_path: Path,
) -> None:
    repository, home, fake_bin, systemctl_log = _registration_fixture(
        tmp_path,
        requires_code_worker=False,
    )
    stale_worker_env = home / ".config" / "chatcopilot-console" / "starter-code-worker.env"

    result = _run_register(repository, home, fake_bin, systemctl_log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert stale_worker_env.read_text(encoding="utf-8") == "PRESERVED=1\n"
    assert not (home / ".config/systemd/user/chatcopilot-code-worker@.service").exists()
    main_env = home / ".config/chatcopilot-console/starter.env"
    assert "CHATCOPILOT_CODE_WORKER_REQUIRED=0" in main_env.read_text(encoding="utf-8")
    calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    worker_unit = "chatcopilot-code-worker" + "@" + "starter.service"
    main_unit = "chatcopilot" + "@" + "starter"
    worker_enable_unit = "chatcopilot-code-worker" + "@" + "starter"
    assert f"--user stop {worker_unit}" in calls
    assert f"--user disable {worker_unit}" in calls
    assert f"--user enable {main_unit}" in calls
    assert not any(call == f"--user enable {worker_enable_unit}" for call in calls)
    assert "注册完成" in result.stdout


def test_registration_generates_and_enables_worker_only_for_code_task_pack(
    tmp_path: Path,
) -> None:
    repository, home, fake_bin, systemctl_log = _registration_fixture(
        tmp_path,
        requires_code_worker=True,
    )

    result = _run_register(repository, home, fake_bin, systemctl_log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / ".config/systemd/user/chatcopilot-code-worker@.service").is_file()
    worker_env = home / ".config/chatcopilot-console/starter-code-worker.env"
    assert "CHATCOPILOT_SOURCE_ROOT" in worker_env.read_text(encoding="utf-8")
    main_env = home / ".config/chatcopilot-console/starter.env"
    assert "CHATCOPILOT_CODE_WORKER_REQUIRED=1" in main_env.read_text(encoding="utf-8")
    calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    main_unit = "chatcopilot" + "@" + "starter"
    worker_prefix = "chatcopilot-code-worker" + "@"
    assert f"--user enable {main_unit}" in calls
    assert f"--user enable {worker_prefix}starter" in calls
    assert not any(f" stop {worker_prefix}" in call for call in calls)
    assert not any(f" disable {worker_prefix}" in call for call in calls)


def test_registration_enable_failure_is_nonzero_without_success_receipt(
    tmp_path: Path,
) -> None:
    repository, home, fake_bin, systemctl_log = _registration_fixture(
        tmp_path,
        requires_code_worker=False,
    )

    result = _run_register(
        repository,
        home,
        fake_bin,
        systemctl_log,
        fail_main_enable=True,
    )

    assert result.returncode != 0
    assert "enable chatcopilot@starter 失败" in result.stderr
    assert "注册完成" not in result.stdout


def test_deploy_all_delegates_the_complete_bot_lifecycle_once() -> None:
    script = (REPO_ROOT / "deploy/wsl/deploy_all.sh").read_text(encoding="utf-8")

    assert 'update_instance.sh" --instance "$bot" --enable' in script
    assert 'console/systemd/register.sh" --enable "$bot"' not in script
    assert 'console/scripts/ctl.sh" start "$bot"' not in script


def test_status_marks_worker_not_applicable_without_code_task_pack() -> None:
    script = (REPO_ROOT / "deploy/wsl/status.sh").read_text(encoding="utf-8")

    assert '"dev.code_tasks" in packs' in script
    assert "not_applicable（BotSpec 未启用 dev.code_tasks）" in script
