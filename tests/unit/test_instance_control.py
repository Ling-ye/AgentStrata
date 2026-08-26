from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from chatcopilot.middleware.acp.instance_control import (
    InstanceControl,
    InstanceControlError,
    RestartHandle,
    validate_instance_id,
)

_TARGET_UNIT = "chatcopilot" + "@" + "qq-bot.service"


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _status_output(
    *,
    load: str = "loaded",
    active: str = "active",
    sub: str = "running",
    pid: int = 321,
    since: str = "Tue 2026-08-25 12:00:00 CST",
    invocation: str = "a" * 32,
) -> str:
    return "\n".join(
        (
            f"LoadState={load}",
            f"ActiveState={active}",
            f"SubState={sub}",
            f"MainPID={pid}",
            f"ActiveEnterTimestamp={since}",
            f"InvocationID={invocation}",
            "",
        )
    )


class _Runner:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), dict(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, subprocess.CompletedProcess)
        return response


def _which(name: str) -> str | None:
    return {
        "systemctl": "/usr/bin/systemctl",
        "systemd-run": "/usr/bin/systemd-run",
    }.get(name)


@pytest.mark.parametrize(
    "value",
    (
        "",
        "a",
        "Upper",
        " leading",
        "trailing ",
        "bot.name",
        "bot/name",
        "bot@name",
        "bot-",
        "bot--name",
        "bot;reboot",
        "bot\nname",
        "a" * 64,
    ),
)
def test_validate_instance_id_rejects_untrusted_unit_fragments(value: str) -> None:
    with pytest.raises(InstanceControlError) as raised:
        validate_instance_id(value)

    assert raised.value.code == "invalid_instance_id"


def test_status_uses_exact_systemctl_argv_and_parses_generation() -> None:
    runner = _Runner(_completed(stdout=_status_output()))

    result = InstanceControl(runner=runner, which=_which).status("qq-bot")

    assert result.instance_id == "qq-bot"
    assert result.unit == _TARGET_UNIT
    assert result.running is True
    assert result.main_pid == 321
    assert result.invocation_id == "a" * 32
    command, kwargs = runner.calls[0]
    assert command[:4] == ["/usr/bin/systemctl", "--user", "show", "--no-pager"]
    assert command[-1] == _TARGET_UNIT
    assert "--property=InvocationID" in command
    assert kwargs["shell"] is False
    assert kwargs["env"]["XDG_RUNTIME_DIR"].startswith("/run/user/")


def test_status_rejects_incomplete_or_failed_systemd_evidence() -> None:
    failed = InstanceControl(
        runner=_Runner(_completed(returncode=1, stderr="unit unavailable")),
        which=_which,
    )
    with pytest.raises(InstanceControlError) as raised:
        failed.status("qq-bot")
    assert raised.value.code == "instance_status_failed"

    incomplete = InstanceControl(
        runner=_Runner(_completed(stdout="LoadState=loaded\nActiveState=active\n")),
        which=_which,
    )
    with pytest.raises(InstanceControlError) as raised:
        incomplete.status("qq-bot")
    assert raised.value.code == "instance_status_invalid"


def test_status_fails_before_runner_when_systemctl_is_unavailable() -> None:
    runner = _Runner()

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=lambda _name: None).status("qq-bot")

    assert raised.value.code == "systemctl_unavailable"
    assert runner.calls == []


def test_restart_preflight_proves_scheduler_and_running_target_without_mutation() -> None:
    runner = _Runner(_completed(stdout=_status_output()))

    status = InstanceControl(runner=runner, which=_which).preflight_restart("qq-bot")

    assert status.running is True
    assert len(runner.calls) == 1
    assert runner.calls[0][0][0:3] == ["/usr/bin/systemctl", "--user", "show"]


def test_restart_preflight_fails_before_status_when_scheduler_is_unavailable() -> None:
    runner = _Runner()

    def missing_systemd_run(name: str) -> str | None:
        return "/usr/bin/systemctl" if name == "systemctl" else None

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=missing_systemd_run).preflight_restart(
            "qq-bot"
        )

    assert raised.value.code == "systemd-run_unavailable"
    assert runner.calls == []


def test_schedule_restart_creates_independent_delayed_timer_without_shell() -> None:
    runner = _Runner(
        _completed(stdout=_status_output()),
        _completed(stdout="Running timer as unit test.timer"),
    )
    handle = InstanceControl(runner=runner, which=_which).schedule_restart(
        "qq-bot",
        delay_seconds=7,
    )

    assert handle == RestartHandle(
        instance_id="qq-bot",
        target_unit=_TARGET_UNIT,
        transient_unit="chatcopilot-restart-qq-bot",
        timer_unit="chatcopilot-restart-qq-bot.timer",
        worker_unit="chatcopilot-restart-qq-bot.service",
        delay_seconds=7,
        previous_main_pid=321,
        previous_active_enter_timestamp="Tue 2026-08-25 12:00:00 CST",
        previous_invocation_id="a" * 32,
    )
    command, kwargs = runner.calls[1]
    assert command[:3] == ["/usr/bin/systemd-run", "--user", "--collect"]
    assert f"--unit={handle.transient_unit}" in command
    assert "--on-active=7s" in command
    assert "--timer-property=AccuracySec=1s" in command
    assert command[-4:] == [
        "/usr/bin/systemctl",
        "--user",
        "restart",
        _TARGET_UNIT,
    ]
    assert "bash" not in command
    assert "sh" not in command
    assert "-c" not in command
    assert kwargs["shell"] is False
    assert any(item.startswith("--setenv=DBUS_SESSION_BUS_ADDRESS=") for item in command)


def test_schedule_restart_refuses_an_unmanaged_or_stopped_instance() -> None:
    runner = _Runner(
        _completed(
            stdout=_status_output(
                load="not-found",
                active="inactive",
                sub="dead",
                pid=0,
                since="",
                invocation="",
            )
        )
    )

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=_which).schedule_restart("qq-bot")

    assert raised.value.code == "instance_not_running"
    assert len(runner.calls) == 1


@pytest.mark.parametrize("delay", (True, 0, 1, 61, 2.5, "5"))
def test_schedule_restart_rejects_an_unsafe_delay_without_running_commands(
    delay: object,
) -> None:
    runner = _Runner()

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=_which).schedule_restart(
            "qq-bot",
            delay_seconds=delay,  # type: ignore[arg-type]
        )

    assert raised.value.code == "invalid_restart_delay"
    assert runner.calls == []


def test_schedule_restart_fails_closed_when_timer_registration_fails() -> None:
    runner = _Runner(
        _completed(stdout=_status_output()),
        _completed(returncode=23, stderr="Failed to start transient timer"),
    )

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=_which).schedule_restart("qq-bot")

    assert raised.value.code == "restart_schedule_failed"
    assert "Failed to start transient timer" in str(raised.value)
    assert len(runner.calls) == 2


def test_second_pending_restart_collides_on_the_same_transient_unit() -> None:
    runner = _Runner(
        _completed(stdout=_status_output()),
        _completed(),
        _completed(stdout=_status_output()),
        _completed(returncode=1, stderr="Unit chatcopilot-restart-qq-bot.timer exists"),
    )
    control = InstanceControl(runner=runner, which=_which)

    first = control.schedule_restart("qq-bot")
    with pytest.raises(InstanceControlError) as raised:
        control.schedule_restart("qq-bot")

    assert raised.value.code == "restart_schedule_failed"
    assert first.transient_unit == "chatcopilot-restart-qq-bot"
    assert f"--unit={first.transient_unit}" in runner.calls[1][0]
    assert f"--unit={first.transient_unit}" in runner.calls[3][0]


def test_schedule_restart_has_no_fallback_when_systemd_run_is_missing() -> None:
    runner = _Runner()

    def missing_systemd_run(name: str) -> str | None:
        return "/usr/bin/systemctl" if name == "systemctl" else None

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=missing_systemd_run).schedule_restart("qq-bot")

    assert raised.value.code == "systemd-run_unavailable"
    assert runner.calls == []


def test_cancel_stops_timer_and_worker_but_never_claims_queued_restart_withdrawal() -> None:
    runner = _Runner(
        _completed(stdout=_status_output()),
        _completed(),
        _completed(),
        _completed(stdout=_status_output()),
    )
    control = InstanceControl(runner=runner, which=_which)
    handle = control.schedule_restart("qq-bot")

    with pytest.raises(InstanceControlError) as raised:
        control.cancel(handle)

    assert raised.value.code == "restart_cancel_unproven"
    assert "可能已有排队" in str(raised.value)
    command, kwargs = runner.calls[2]
    assert command == [
        "/usr/bin/systemctl",
        "--user",
        "stop",
        handle.timer_unit,
        handle.worker_unit,
    ]
    assert kwargs["shell"] is False


def test_cancel_reports_when_restart_generation_already_changed() -> None:
    runner = _Runner(
        _completed(stdout=_status_output()),
        _completed(),
        _completed(),
        _completed(stdout=_status_output(pid=654, invocation="d" * 32)),
    )
    control = InstanceControl(runner=runner, which=_which)
    handle = control.schedule_restart("qq-bot")

    with pytest.raises(InstanceControlError) as raised:
        control.cancel(handle)

    assert raised.value.code == "restart_cancel_unproven"
    assert "generation 已变化" in str(raised.value)


def test_cancel_fails_closed_when_systemd_cannot_stop_the_timer() -> None:
    runner = _Runner(
        _completed(stdout=_status_output()),
        _completed(),
        _completed(returncode=5, stderr="Failed to stop timer"),
    )
    control = InstanceControl(runner=runner, which=_which)
    handle = control.schedule_restart("qq-bot")

    with pytest.raises(InstanceControlError) as raised:
        control.cancel(handle)

    assert raised.value.code == "restart_cancel_failed"
    assert len(runner.calls) == 3


def test_cancel_rejects_a_forged_handle_before_systemctl() -> None:
    runner = _Runner(_completed(stdout=_status_output()), _completed())
    control = InstanceControl(runner=runner, which=_which)
    handle = control.schedule_restart("qq-bot")
    calls_before_cancel = len(runner.calls)

    with pytest.raises(InstanceControlError) as raised:
        control.cancel(replace(handle, timer_unit="--now.timer"))

    assert raised.value.code == "invalid_restart_handle"
    assert len(runner.calls) == calls_before_cancel


def test_runner_exceptions_are_structured_and_do_not_trigger_fallback() -> None:
    runner = _Runner(OSError("user bus unavailable"))

    with pytest.raises(InstanceControlError) as raised:
        InstanceControl(runner=runner, which=_which).status("qq-bot")

    assert raised.value.code == "instance_status_failed"
    assert "OSError" in str(raised.value)
    assert len(runner.calls) == 1
