"""Validate rendered delivery units with systemd's parser, without starting services."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SERVICE = "agentstrata-harness-delivery.service"
TIMER = "agentstrata-harness-delivery.timer"


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd parser unavailable")
@pytest.mark.parametrize("directory", ["repository", "repository with spaces"])
def test_rendered_delivery_units_pass_systemd_verification(tmp_path, directory):
    repository = tmp_path / directory
    templates = repository / "console/systemd"
    templates.mkdir(parents=True)
    for name in (SERVICE, TIMER):
        shutil.copyfile(ROOT / "console/systemd" / name, templates / name)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config = tmp_path / "config with spaces" / "harness.env"
    environment = {**os.environ, "CHATCOPILOT_HARNESS_ENV": str(config),
                   "XDG_RUNTIME_DIR": str(runtime)}
    rendered = subprocess.run(
        [sys.executable, str(ROOT / "scripts/install_harness_delivery_timer.py"),
         "--repository", str(repository), "--dry-run"],
        env=environment, capture_output=True, text=True, check=True,
    )
    service_text, separator, timer_text = rendered.stdout.removeprefix(SERVICE + "\n").partition(TIMER + "\n")
    assert separator
    assert f'Environment="CHATCOPILOT_HARNESS_ENV={config}"' in service_text
    units = tmp_path / "units"
    units.mkdir()
    (units / SERVICE).write_text(service_text)
    (units / TIMER).write_text(timer_text)
    verified = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(units / SERVICE), str(units / TIMER)],
        env=environment, capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="Console setup requires Bash")
@pytest.mark.parametrize("timer_status", [0, 1])
def test_console_completion_depends_on_timer_start(timer_status):
    script = (ROOT / "console/setup_console.sh").read_text()
    # Execute the real final setup stage with a controlled systemctl result.
    final_stage = script[script.rindex("\ntrap - EXIT"):]
    functions = r'''
ok() { printf '[OK] %s\n' "$*"; }
err() { printf '[ERR] %s\n' "$*" >&2; }
print_service_diagnostics() { echo diagnostics >&2; }
systemctl() { printf 'systemctl:%s\n' "$*"; return "$TIMER_STATUS"; }
'''
    result = subprocess.run(
        ["bash", "-c", functions + final_stage],
        env={**os.environ, "TIMER_STATUS": str(timer_status)}, capture_output=True, text=True,
    )
    assert "systemctl:--user start " + TIMER in result.stdout
    if timer_status:
        assert result.returncode == 1
        assert "完成。浏览器打开" not in result.stdout
        assert "Harness PR 对账定时器启动失败" in result.stderr
        assert "diagnostics" in result.stderr
    else:
        assert result.returncode == 0
        assert result.stdout.index("systemctl:") < result.stdout.index("完成。浏览器打开")
