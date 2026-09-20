import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _deploy_environment(
    tmp_path: Path,
    *,
    bus_mode: str,
    package_ready: bool = True,
    pid1: str = "systemd",
) -> tuple[Path, dict[str, str], Path]:
    repository = tmp_path / "repo"
    script = repository / "deploy/wsl/deploy_console.sh"
    script.parent.mkdir(parents=True)
    script.write_text(_read("deploy/wsl/deploy_console.sh"), encoding="utf-8")
    script.chmod(0o755)
    (repository / "console").mkdir()
    (repository / "src/chatcopilot").mkdir(parents=True)

    fake_python = repository / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    _write_executable(fake_python, "#!/usr/bin/env bash\nexit 0\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    bus_ready = tmp_path / "bus-ready"
    if bus_mode == "healthy":
        bus_ready.touch()

    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$CALL_LOG"
case "$*" in
  "--user show-environment") [ -f "$BUS_READY" ]; exit $? ;;
  "restart user@"*.service)
    if [ "$BUS_MODE" = restart_success ]; then touch "$BUS_READY"; exit 0; fi
    exit 1
    ;;
  "reset-failed user@"*.service) exit 0 ;;
  "start user@"*.service)
    if [ "$BUS_MODE" = fallback_success ]; then touch "$BUS_READY"; fi
    exit 0
    ;;
  "--user is-active --quiet chatcopilot-evaluation.service") exit 0 ;;
  "--user is-active --quiet chatcopilot-console.service") exit 0 ;;
  "--user restart chatcopilot-console.service") exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "sudo",
        """#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "$CALL_LOG"
if [ "$*" = "-n -v" ] || [ "$*" = "-v" ]; then exit 0; fi
if [ "${1:-}" = "-n" ]; then shift; fi
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "dpkg",
        """#!/usr/bin/env bash
[ "$PACKAGE_READY" = 1 ]
""",
    )
    _write_executable(
        fake_bin / "ps",
        """#!/usr/bin/env bash
printf '%s\n' "$PID1_NAME"
""",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "python3", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")

    environment = os.environ.copy()
    environment.update(
        {
            "BUS_MODE": bus_mode,
            "BUS_READY": str(bus_ready),
            "CALL_LOG": str(call_log),
            "PACKAGE_READY": "1" if package_ready else "0",
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PID1_NAME": pid1,
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
    )
    return script, environment, call_log


def _run_deploy(
    script: Path,
    environment: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_all_system_package_bootstraps_install_user_dbus() -> None:
    assert "dbus-user-session" in _read("deploy/wsl/install_wsl_env.sh")
    assert "dbus-user-session" in _read("deploy/wsl/setup_wsl_root.sh")


def test_console_setup_fails_closed_when_user_bus_is_unavailable() -> None:
    script = _read("console/setup_console.sh")

    assert "dpkg -s dbus-user-session" in script
    assert "systemctl --user is-system-running" in script
    assert "systemctl reset-failed user@$uid.service" in script
    assert "systemctl start user@$uid.service" in script


def test_deploy_does_not_use_sudo_when_user_bus_is_healthy(tmp_path: Path) -> None:
    script, environment, call_log = _deploy_environment(tmp_path, bus_mode="healthy")

    completed = _run_deploy(script, environment, "--restart-only", "--skip-web")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "sudo " not in calls
    assert "systemctl --user restart chatcopilot-console.service" in calls


def test_deploy_recovers_user_bus_with_restart(tmp_path: Path) -> None:
    script, environment, call_log = _deploy_environment(
        tmp_path,
        bus_mode="restart_success",
    )

    completed = _run_deploy(script, environment, "--restart-only", "--skip-web")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "sudo -n -v" in calls
    assert "systemctl restart user@" in calls
    assert "systemctl reset-failed user@" not in calls
    assert "systemd --user 已自动恢复" in completed.stdout


def test_deploy_falls_back_to_reset_and_start_after_restart_failure(
    tmp_path: Path,
) -> None:
    script, environment, call_log = _deploy_environment(
        tmp_path,
        bus_mode="fallback_success",
    )

    completed = _run_deploy(script, environment, "--restart-only", "--skip-web")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    restart = calls.index("systemctl restart user@")
    reset = calls.index("systemctl reset-failed user@")
    start = calls.index("systemctl start user@")
    console = calls.index("systemctl --user restart chatcopilot-console.service")
    assert restart < reset < start < console


def test_deploy_stops_before_service_operations_when_recovery_does_not_help(
    tmp_path: Path,
) -> None:
    script, environment, call_log = _deploy_environment(
        tmp_path,
        bus_mode="never_ready",
    )

    completed = _run_deploy(script, environment, "--restart-only", "--skip-web")

    assert completed.returncode == 1
    calls = call_log.read_text(encoding="utf-8")
    assert "systemctl start user@" in calls
    assert "systemctl --user restart chatcopilot-console.service" not in calls
    assert "自动恢复后仍不可达" in completed.stderr


def test_deploy_status_is_read_only_when_user_bus_is_unavailable(tmp_path: Path) -> None:
    script, environment, call_log = _deploy_environment(
        tmp_path,
        bus_mode="never_ready",
    )

    completed = _run_deploy(script, environment, "--status")

    assert completed.returncode == 1
    calls = call_log.read_text(encoding="utf-8")
    assert "sudo " not in calls
    assert "systemctl restart user@" not in calls
    assert "systemd user bus 不可达" in completed.stderr


def test_deploy_dry_run_only_prints_user_bus_recovery(tmp_path: Path) -> None:
    script, environment, call_log = _deploy_environment(
        tmp_path,
        bus_mode="never_ready",
    )

    completed = _run_deploy(
        script,
        environment,
        "--restart-only",
        "--skip-web",
        "--dry-run",
    )

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "sudo " not in calls
    assert "systemctl restart user@" not in calls
    assert "[DRY-RUN] sudo systemctl restart user@" in completed.stdout
    assert "[DRY-RUN] systemctl --user restart chatcopilot-console.service" in completed.stdout


@pytest.mark.parametrize(
    ("package_ready", "pid1", "expected"),
    (
        (False, "systemd", "缺少 dbus-user-session"),
        (True, "init", "PID 1 不是 systemd"),
    ),
)
def test_deploy_does_not_attempt_recovery_when_prerequisites_are_missing(
    tmp_path: Path,
    package_ready: bool,
    pid1: str,
    expected: str,
) -> None:
    script, environment, call_log = _deploy_environment(
        tmp_path,
        bus_mode="never_ready",
        package_ready=package_ready,
        pid1=pid1,
    )

    completed = _run_deploy(script, environment, "--restart-only", "--skip-web")

    assert completed.returncode == 1
    calls = call_log.read_text(encoding="utf-8")
    assert "sudo " not in calls
    assert "systemctl restart user@" not in calls
    assert "systemctl --user restart chatcopilot-console.service" not in calls
    assert expected in completed.stderr
