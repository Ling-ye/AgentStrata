from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from console.control import operations


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_evaluation_unit_uses_private_unix_socket_and_preserves_workers() -> None:
    unit = _read("console/systemd/chatcopilot-evaluation.service")

    assert "-m chatcopilot.evals.service serve" in unit
    assert "--socket %t/agentstrata-evaluation/service.sock" in unit
    assert "RuntimeDirectory=agentstrata-evaluation" in unit
    assert "RuntimeDirectoryMode=0700" in unit
    assert "UMask=0077" in unit
    assert (
        "Environment=CHATCOPILOT_EVALUATION_SOCKET=%t/agentstrata-evaluation/service.sock" in unit
    )
    assert "KillMode=process" in unit
    assert "NoNewPrivileges=true" in unit
    assert "PrivateTmp=true" in unit


def test_console_unit_waits_for_but_does_not_own_evaluation_service() -> None:
    unit = _read("console/systemd/chatcopilot-console.service")

    assert "Wants=chatcopilot-evaluation.service" in unit
    assert "After=default.target chatcopilot-evaluation.service" in unit
    assert (
        "Environment=CHATCOPILOT_EVALUATION_SOCKET=%t/agentstrata-evaluation/service.sock" in unit
    )
    assert "Requires=chatcopilot-evaluation.service" not in unit
    assert "PartOf=chatcopilot-evaluation.service" not in unit
    assert "BindsTo=chatcopilot-evaluation.service" not in unit


def test_console_setup_starts_evaluation_before_console() -> None:
    script = _read("console/setup_console.sh")

    assert 'EVALUATION_UNIT_NAME="chatcopilot-evaluation.service"' in script
    assert "import chatcopilot.evals.service" in script
    assert 'chmod 700 "$EVALUATION_ROOT"' in script
    assert 'systemctl --user enable "$EVALUATION_UNIT_NAME" "$UNIT_NAME"' in script

    evaluation_restart = script.index('systemctl --user restart "$EVALUATION_UNIT_NAME"')
    evaluation_health = script.index("if ! wait_for_evaluation", evaluation_restart)
    console_restart = script.index(
        'systemctl --user restart "$UNIT_NAME"',
        evaluation_health,
    )
    assert evaluation_restart < evaluation_health < console_restart


def test_deploy_restart_only_does_not_restart_evaluation() -> None:
    script = _read("deploy/wsl/deploy_console.sh")
    restart_branch = script.split(
        'if [ "$RESTART_ONLY" -eq 1 ]; then',
        maxsplit=1,
    )[1].split(
        'elif [ "$UPDATE_ONLY" -eq 1 ]; then',
        maxsplit=1,
    )[0]

    assert "restart_console" in restart_branch
    assert "restart_evaluation" not in restart_branch


def test_deploy_update_restarts_evaluation_then_console() -> None:
    script = _read("deploy/wsl/deploy_console.sh")
    update_branch = script.split(
        'elif [ "$UPDATE_ONLY" -eq 1 ]; then',
        maxsplit=1,
    )[1].split("else", maxsplit=1)[0]

    maintenance_enter = update_branch.index("maintenance_enter")
    build = update_branch.index("build_web")
    evaluation = update_branch.index("restart_evaluation")
    console = update_branch.index("restart_console")
    maintenance_leave = update_branch.index("maintenance_leave")
    assert maintenance_enter < build < evaluation < console < maintenance_leave
    assert "idle cannot be proven; update refused" in update_branch


def test_health_cli_and_setup_refuse_code_update_while_evaluation_is_active() -> None:
    health_cli = _read("src/chatcopilot/evals/service/__main__.py")
    setup = _read("console/setup_console.sh")

    assert '"--require-idle"' in health_cli
    assert "active_count" in health_cli
    assert "maintenance enter" in setup
    idle_check = setup.index("maintenance_enter")
    dependency_update = setup.index('info "安装控制台依赖..."')
    assert idle_check < dependency_update


def test_setup_refuses_installed_but_inactive_evaluation_service_before_updates(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    _write_executable(fake_bin / "dpkg", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$CALL_LOG"
case "$*" in
  "--user is-system-running") printf 'running\n'; exit 0 ;;
  "--user cat chatcopilot-evaluation.service") exit 0 ;;
  "--user is-active --quiet chatcopilot-evaluation.service") exit 3 ;;
  *) exit 0 ;;
esac
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "CALL_LOG": str(call_log),
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
    )

    completed = subprocess.run(
        ["bash", str(REPO_ROOT / "console/setup_console.sh"), "--skip-web"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "已安装但未运行" in completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "is-active --quiet chatcopilot-evaluation.service" in calls
    assert "restart" not in calls


def _deploy_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repository = tmp_path / "repo"
    script = repository / "deploy/wsl/deploy_console.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        _read("deploy/wsl/deploy_console.sh"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (repository / "console").mkdir()
    (repository / "src/chatcopilot").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    fake_python = repository / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
printf 'venv-python %s\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"maintenance enter"*)
    if [ "${FAIL_MAINTENANCE_ENTER:-0}" = 1 ]; then exit 1; fi
    printf '0123456789abcdef0123456789abcdef\n'
    ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "python3",
        """#!/usr/bin/env bash
printf 'python3 %s\n' "$*" >> "$CALL_LOG"
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$CALL_LOG"
if [ "${FAIL_EVALUATION_RESTART:-0}" = 1 ] &&
   [ "$*" = "--user restart chatcopilot-evaluation.service" ]; then
  exit 1
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$CALL_LOG"
exit 0
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "CALL_LOG": str(call_log),
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
    )
    return script, environment, call_log


def test_deploy_holds_maintenance_across_both_service_restarts(
    tmp_path: Path,
) -> None:
    script, environment, call_log = _deploy_harness(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--update-only", "--skip-web"],
        cwd=script.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    enter = calls.index("maintenance enter")
    evaluation_restart = calls.index("systemctl --user restart chatcopilot-evaluation.service")
    console_restart = calls.index("systemctl --user restart chatcopilot-console.service")
    leave = calls.index("maintenance leave")
    assert enter < evaluation_restart < console_restart < leave


def test_deploy_refuses_updates_when_maintenance_cannot_be_acquired(
    tmp_path: Path,
) -> None:
    script, environment, call_log = _deploy_harness(tmp_path)
    environment["FAIL_MAINTENANCE_ENTER"] = "1"

    completed = subprocess.run(
        ["bash", str(script), "--update-only", "--skip-web"],
        cwd=script.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    calls = call_log.read_text(encoding="utf-8")
    assert "maintenance enter" in calls
    assert "restart chatcopilot-evaluation.service" not in calls
    assert "restart chatcopilot-console.service" not in calls


def test_deploy_failure_path_releases_acquired_maintenance(
    tmp_path: Path,
) -> None:
    script, environment, call_log = _deploy_harness(tmp_path)
    environment["FAIL_EVALUATION_RESTART"] = "1"

    completed = subprocess.run(
        ["bash", str(script), "--update-only", "--skip-web"],
        cwd=script.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    calls = call_log.read_text(encoding="utf-8")
    assert calls.index("maintenance enter") < calls.index("maintenance leave")


def _console_update_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    systemd_exit: int,
) -> tuple[Path, Path]:
    repository = tmp_path / "repo"
    deploy_script = repository / "deploy/wsl/deploy_console.sh"
    deploy_script.parent.mkdir(parents=True)
    lease_marker = tmp_path / "maintenance-acquired"
    call_log = tmp_path / "console-update-calls.log"
    _write_executable(
        deploy_script,
        """#!/usr/bin/env bash
printf 'deploy %s\n' "$*" >> "$CALL_LOG"
touch "$LEASE_MARKER"
""",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "systemd-run",
        f"""#!/usr/bin/env bash
printf 'systemd-run %s\n' "$*" >> "$CALL_LOG"
exit {systemd_exit}
""",
    )
    _write_executable(
        fake_bin / "setsid",
        """#!/usr/bin/env bash
printf 'setsid %s\n' "$*" >> "$CALL_LOG"
exec "$@"
""",
    )
    monkeypatch.setattr(operations, "repo_root", lambda: repository)
    monkeypatch.setenv("CALL_LOG", str(call_log))
    monkeypatch.setenv("LEASE_MARKER", str(lease_marker))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    return call_log, lease_marker


def test_console_update_uses_independent_systemd_transient_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_log, lease_marker = _console_update_harness(
        tmp_path,
        monkeypatch,
        systemd_exit=0,
    )

    result = operations.trigger_console_update()

    assert result["ok"] is True
    assert result["mode"] == "systemd-run"
    calls = call_log.read_text(encoding="utf-8")
    assert "systemd-run --user --collect --unit=cc-console-update-" in calls
    assert "--property=Type=exec" in calls
    assert "setsid" not in calls
    assert not lease_marker.exists()


def test_console_update_fails_closed_without_transient_unit_and_starts_no_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_log, lease_marker = _console_update_harness(
        tmp_path,
        monkeypatch,
        systemd_exit=23,
    )

    result = operations.trigger_console_update()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        calls = call_log.read_text(encoding="utf-8")
        if "setsid" in calls or lease_marker.exists():
            break
        time.sleep(0.01)

    assert result["ok"] is False
    assert "独立 systemd transient unit" in str(result["error"])
    assert "未获取 Evaluation maintenance lease" in str(result["error"])
    assert "deploy/wsl/deploy_console.sh --update-only" in str(result["error"])
    calls = call_log.read_text(encoding="utf-8")
    assert "systemd-run" in calls
    assert "setsid" not in calls
    assert "deploy --update-only" not in calls
    assert not lease_marker.exists()


def test_deploy_status_checks_direct_socket_and_console_bff() -> None:
    script = _read("deploy/wsl/deploy_console.sh")

    assert 'info "checking $EVALUATION_UNIT_NAME ..."' in script
    assert "-m chatcopilot.evals.service health" in script
    assert 'EVALUATION_BFF_URL="http://127.0.0.1:8910/api/evals/health"' in script
    assert 'wait_for_http "$EVALUATION_BFF_URL"' in script
