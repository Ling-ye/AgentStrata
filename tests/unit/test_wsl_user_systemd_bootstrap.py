from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_all_system_package_bootstraps_install_user_dbus() -> None:
    assert "dbus-user-session" in _read("deploy/wsl/install_wsl_env.sh")
    assert "dbus-user-session" in _read("deploy/wsl/setup_wsl_root.sh")


def test_console_setup_fails_closed_when_user_bus_is_unavailable() -> None:
    script = _read("console/setup_console.sh")

    assert "dpkg -s dbus-user-session" in script
    assert "systemctl --user is-system-running" in script
    assert "systemctl reset-failed user@$uid.service" in script
    assert "systemctl start user@$uid.service" in script
