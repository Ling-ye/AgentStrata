"""Regression coverage for the GC systemd unit renderer.

These tests exercise ``GovernanceScheduler`` itself and replace only its
systemctl boundary.  The generated units are then given to systemd's parser.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.governance_types import GovernanceOptions, GovernanceSchedule
from chatcopilot.harness.schedule_runtime import GovernanceScheduler


def _settings(*, enabled: bool = True) -> GovernanceSchedule:
    return GovernanceSchedule(enabled, 24, GovernanceOptions("fixture"))


def _scheduler(tmp_path: Path, *, command=None) -> tuple[GovernanceScheduler, SimpleNamespace, Mock]:
    repository = tmp_path / "agentstrata-repository"
    repository.mkdir(parents=True)
    systemctl = command or Mock(return_value=subprocess.CompletedProcess(
        [], 0, "LoadState=loaded\nActiveState=active\n", "",
    ))
    controller = SimpleNamespace(
        repository=repository,
        active_governance_run=Mock(return_value=None),
        store=SimpleNamespace(root=tmp_path / "private", active_governance=Mock(return_value=None)),
        start_code_health=Mock(return_value={"run_id": "gc-example"}),
    )
    return GovernanceScheduler(controller, unit_directory=tmp_path / "units", command=systemctl,
                               clock=lambda: 86_400), controller, systemctl


def _rendered_units(tmp_path: Path) -> tuple[GovernanceScheduler, SimpleNamespace, Mock, str, str]:
    runtime, controller, systemctl = _scheduler(tmp_path)
    runtime.configure(_settings())
    return (
        runtime,
        controller,
        systemctl,
        (runtime.units / f"{runtime.unit}.service").read_text(),
        (runtime.units / f"{runtime.unit}.timer").read_text(),
    )


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd parser unavailable")
def test_gc_schedule_renders_a_valid_unquoted_absolute_working_directory(tmp_path: Path) -> None:
    """A systemd unit must not quote the absolute path assigned to WorkingDirectory."""
    runtime, _controller, _systemctl, service, timer = _rendered_units(tmp_path)
    repository = tmp_path / "agentstrata-repository"

    assert repository.is_absolute()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    verified = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(runtime.units / f"{runtime.unit}.service"),
         str(runtime.units / f"{runtime.unit}.timer")],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)},
        capture_output=True,
        text=True,
    )
    # Sandboxed CI may deny the user manager socket even though the parser has
    # inspected the unit.  Check its syntax diagnostics rather than treating
    # that host limitation as a product failure.
    diagnostics = verified.stdout + verified.stderr
    assert "WorkingDirectory= path is not absolute" not in diagnostics, diagnostics
    assert "Unit configuration has fatal error" not in diagnostics, diagnostics
    assert f"Unit {runtime.unit}.service has a bad unit file setting" not in diagnostics, diagnostics
    assert f"WorkingDirectory={repository}\n" in service
    assert f'WorkingDirectory="{repository}"' not in service


def test_gc_schedule_keeps_command_environment_timer_and_dispatch_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The field-specific repair does not alter the scheduler's existing contracts."""
    monkeypatch.setenv("CHATCOPILOT_HARNESS_ENV", 'value with "quotes" and %')
    runtime, controller, systemctl, service, timer = _rendered_units(tmp_path)
    repository = tmp_path / "agentstrata-repository"

    assert f'Environment="PYTHONPATH={repository / "src"}"' in service
    assert 'Environment="CHATCOPILOT_HARNESS_ENV=value with \\"quotes\\" and %%"' in service
    assert 'ExecStart="' in service
    assert '"--repository-root"' in service
    assert '"gc-tick"' in service
    assert "OnUnitActiveSec=24h" in timer

    first = runtime.tick()
    second = GovernanceScheduler(controller, unit_directory=runtime.units, command=systemctl,
                                 clock=runtime.clock).tick()
    assert second["duplicate"] is True
    assert second["run_id"] == first["run_id"]
    controller.start_code_health.assert_called_once()

    unavailable = Mock(return_value=subprocess.CompletedProcess([], 1, "", "unavailable"))
    failed_runtime, failed_controller, _ = _scheduler(tmp_path / "activation-failure", command=unavailable)
    with pytest.raises(HarnessError):
        failed_runtime.configure(_settings())
    assert failed_runtime.store.read()["enabled"] is False
    assert failed_runtime.tick() == {"status": "disabled"}
    failed_controller.start_code_health.assert_not_called()
