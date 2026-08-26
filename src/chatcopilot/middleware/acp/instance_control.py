"""Fail-closed systemd control for the BotSpec-bound runtime instance."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable


_INSTANCE_ID_RE = re.compile(r"(?=.{2,63}\Z)[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_STATUS_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ActiveEnterTimestamp",
    "InvocationID",
)

DEFAULT_RESTART_DELAY_SECONDS = 5
MIN_RESTART_DELAY_SECONDS = 2
MAX_RESTART_DELAY_SECONDS = 60

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
WhichCommand = Callable[[str], str | None]


class InstanceControlError(RuntimeError):
    """Structured failure returned before an unsafe or unproven lifecycle action."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InstanceServiceStatus:
    instance_id: str
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int
    active_enter_timestamp: str
    invocation_id: str

    @property
    def running(self) -> bool:
        return self.load_state == "loaded" and self.active_state == "active" and self.main_pid > 0


@dataclass(frozen=True)
class RestartHandle:
    instance_id: str
    target_unit: str
    transient_unit: str
    timer_unit: str
    worker_unit: str
    delay_seconds: int
    previous_main_pid: int
    previous_active_enter_timestamp: str
    previous_invocation_id: str


class InstanceControl:
    """Query and schedule one exact ``chatcopilot@<instance>`` user service."""

    def __init__(
        self,
        *,
        runner: RunCommand | None = None,
        which: WhichCommand | None = None,
    ) -> None:
        self._runner = runner or subprocess.run
        self._which = which or shutil.which

    def status(self, instance_id: str) -> InstanceServiceStatus:
        normalized = validate_instance_id(instance_id)
        systemctl = self._require_executable("systemctl")
        return self._status_with_systemctl(normalized, systemctl)

    def preflight_restart(self, instance_id: str) -> InstanceServiceStatus:
        """Prove the detached scheduler and current target are available."""

        previous, _systemctl, _systemd_run = self._restart_preflight(instance_id)
        return previous

    def schedule_restart(
        self,
        instance_id: str,
        *,
        delay_seconds: int = DEFAULT_RESTART_DELAY_SECONDS,
    ) -> RestartHandle:
        delay = _validate_delay(delay_seconds)
        previous, systemctl, systemd_run = self._restart_preflight(instance_id)
        normalized = previous.instance_id

        # One stable transient name per instance makes concurrent restart requests
        # collide in systemd instead of scheduling duplicate restarts.
        transient_unit = f"chatcopilot-restart-{normalized}"
        runtime_dir, bus_address, env = _user_systemd_environment()
        command = [
            systemd_run,
            "--user",
            "--collect",
            f"--unit={transient_unit}",
            f"--description=Restart AgentStrata instance {normalized}",
            f"--on-active={delay}s",
            "--timer-property=AccuracySec=1s",
            "--property=Type=exec",
            f"--setenv=XDG_RUNTIME_DIR={runtime_dir}",
            f"--setenv=DBUS_SESSION_BUS_ADDRESS={bus_address}",
            systemctl,
            "--user",
            "restart",
            previous.unit,
        ]
        completed = self._invoke(
            command,
            env=env,
            timeout=15.0,
            code="restart_schedule_failed",
            message="无法注册独立的延迟重启 timer，未使用进程内或 shell 降级路径。",
        )
        if completed.returncode != 0:
            raise InstanceControlError(
                "restart_schedule_failed",
                f"无法注册独立的延迟重启 timer，未调度重启：{_command_failure_detail(completed)}",
            )
        return RestartHandle(
            instance_id=normalized,
            target_unit=previous.unit,
            transient_unit=transient_unit,
            timer_unit=f"{transient_unit}.timer",
            worker_unit=f"{transient_unit}.service",
            delay_seconds=delay,
            previous_main_pid=previous.main_pid,
            previous_active_enter_timestamp=previous.active_enter_timestamp,
            previous_invocation_id=previous.invocation_id,
        )

    def _restart_preflight(
        self,
        instance_id: str,
    ) -> tuple[InstanceServiceStatus, str, str]:
        normalized = validate_instance_id(instance_id)
        systemctl = self._require_executable("systemctl")
        systemd_run = self._require_executable("systemd-run")
        previous = self._status_with_systemctl(normalized, systemctl)
        if not previous.running:
            raise InstanceControlError(
                "instance_not_running",
                "当前机器人实例不是已加载且正在运行的 systemd user service，未调度重启。",
            )
        return previous, systemctl, systemd_run

    def cancel(self, handle: RestartHandle) -> None:
        """Request that the transient units stop without claiming the target restart was withdrawn."""

        _validate_handle(handle)
        systemctl = self._require_executable("systemctl")
        _runtime_dir, _bus_address, env = _user_systemd_environment()
        completed = self._invoke(
            [
                systemctl,
                "--user",
                "stop",
                handle.timer_unit,
                handle.worker_unit,
            ],
            env=env,
            timeout=15.0,
            code="restart_cancel_failed",
            message="无法撤销延迟重启 timer。",
        )
        if completed.returncode != 0:
            raise InstanceControlError(
                "restart_cancel_failed",
                f"无法撤销延迟重启 timer：{_command_failure_detail(completed)}",
            )

        current = self._status_with_systemctl(handle.instance_id, systemctl)
        detail = (
            "目标实例 generation 已变化"
            if not _same_service_generation(handle, current)
            else "目标实例 generation 暂未变化，但 systemd manager 中可能已有排队的 restart job"
        )
        raise InstanceControlError(
            "restart_cancel_unproven",
            f"已请求停止延迟重启 transient units；{detail}，无法证明重启已撤销。",
        )

    def _status_with_systemctl(
        self,
        instance_id: str,
        systemctl: str,
    ) -> InstanceServiceStatus:
        unit = service_unit(instance_id)
        _runtime_dir, _bus_address, env = _user_systemd_environment()
        command = [
            systemctl,
            "--user",
            "show",
            "--no-pager",
            *(f"--property={name}" for name in _STATUS_PROPERTIES),
            unit,
        ]
        completed = self._invoke(
            command,
            env=env,
            timeout=10.0,
            code="instance_status_failed",
            message="无法读取机器人实例的 systemd 状态。",
        )
        if completed.returncode != 0:
            raise InstanceControlError(
                "instance_status_failed",
                f"无法读取机器人实例的 systemd 状态：{_command_failure_detail(completed)}",
            )
        return _parse_status(instance_id, unit, completed.stdout or "")

    def _require_executable(self, name: str) -> str:
        resolved = self._which(name)
        if (
            not isinstance(resolved, str)
            or not resolved
            or not os.path.isabs(resolved)
            or "\x00" in resolved
            or "\n" in resolved
            or "\r" in resolved
        ):
            raise InstanceControlError(
                f"{name}_unavailable",
                f"{name} 不可用或未解析为绝对路径，未执行实例控制。",
            )
        return resolved

    def _invoke(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        timeout: float,
        code: str,
        message: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstanceControlError(code, f"{message} {type(exc).__name__}: {exc}") from exc


def validate_instance_id(instance_id: str) -> str:
    """Accept only the repository's lowercase kebab-case BotSpec identifier shape."""

    if not isinstance(instance_id, str) or _INSTANCE_ID_RE.fullmatch(instance_id) is None:
        raise InstanceControlError(
            "invalid_instance_id",
            "机器人实例 ID 非法；必须是 2–63 位小写 kebab-case 标识。",
        )
    return instance_id


def service_unit(instance_id: str) -> str:
    return f"chatcopilot@{validate_instance_id(instance_id)}.service"


def status(
    instance_id: str,
    *,
    runner: RunCommand | None = None,
    which: WhichCommand | None = None,
) -> InstanceServiceStatus:
    return InstanceControl(runner=runner, which=which).status(instance_id)


def schedule_restart(
    instance_id: str,
    *,
    delay_seconds: int = DEFAULT_RESTART_DELAY_SECONDS,
    runner: RunCommand | None = None,
    which: WhichCommand | None = None,
) -> RestartHandle:
    return InstanceControl(runner=runner, which=which).schedule_restart(
        instance_id,
        delay_seconds=delay_seconds,
    )


def preflight_restart(
    instance_id: str,
    *,
    runner: RunCommand | None = None,
    which: WhichCommand | None = None,
) -> InstanceServiceStatus:
    return InstanceControl(runner=runner, which=which).preflight_restart(instance_id)


def cancel(
    handle: RestartHandle,
    *,
    runner: RunCommand | None = None,
    which: WhichCommand | None = None,
) -> None:
    InstanceControl(runner=runner, which=which).cancel(handle)


def _validate_delay(delay_seconds: int) -> int:
    if (
        isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, int)
        or not MIN_RESTART_DELAY_SECONDS <= delay_seconds <= MAX_RESTART_DELAY_SECONDS
    ):
        raise InstanceControlError(
            "invalid_restart_delay",
            "重启延迟必须是 2–60 秒的整数。",
        )
    return delay_seconds


def _parse_status(instance_id: str, unit: str, output: str) -> InstanceServiceStatus:
    values: dict[str, str] = {}
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        key, separator, value = raw_line.partition("=")
        if separator != "=" or key not in _STATUS_PROPERTIES or key in values:
            raise InstanceControlError(
                "instance_status_invalid",
                "systemd 返回了无法验证的实例状态。",
            )
        values[key] = value.strip()
    if set(values) != set(_STATUS_PROPERTIES):
        raise InstanceControlError(
            "instance_status_invalid",
            "systemd 返回的实例状态字段不完整。",
        )
    raw_pid = values["MainPID"]
    if re.fullmatch(r"[0-9]+", raw_pid) is None:
        raise InstanceControlError(
            "instance_status_invalid",
            "systemd 返回的实例 MainPID 非法。",
        )
    return InstanceServiceStatus(
        instance_id=instance_id,
        unit=unit,
        load_state=values["LoadState"],
        active_state=values["ActiveState"],
        sub_state=values["SubState"],
        main_pid=int(raw_pid),
        active_enter_timestamp=values["ActiveEnterTimestamp"],
        invocation_id=values["InvocationID"],
    )


def _validate_handle(handle: RestartHandle) -> None:
    if not isinstance(handle, RestartHandle):
        raise InstanceControlError("invalid_restart_handle", "重启撤销句柄非法。")
    instance_id = validate_instance_id(handle.instance_id)
    expected_target = service_unit(instance_id)
    expected_transient = f"chatcopilot-restart-{instance_id}"
    if (
        handle.transient_unit != expected_transient
        or handle.target_unit != expected_target
        or handle.timer_unit != f"{handle.transient_unit}.timer"
        or handle.worker_unit != f"{handle.transient_unit}.service"
        or handle.previous_main_pid <= 0
    ):
        raise InstanceControlError("invalid_restart_handle", "重启撤销句柄与实例不匹配。")
    _validate_delay(handle.delay_seconds)


def _same_service_generation(
    handle: RestartHandle,
    current: InstanceServiceStatus,
) -> bool:
    if not current.running or current.unit != handle.target_unit:
        return False
    if handle.previous_invocation_id:
        return current.invocation_id == handle.previous_invocation_id
    return (
        current.main_pid == handle.previous_main_pid
        and current.active_enter_timestamp == handle.previous_active_enter_timestamp
    )


def _user_systemd_environment() -> tuple[str, str, dict[str, str]]:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise InstanceControlError(
            "systemd_user_environment_unavailable",
            "当前平台无法确定 systemd user manager 身份。",
        )
    runtime_dir = f"/run/user/{getuid()}"
    bus_address = f"unix:path={runtime_dir}/bus"
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["DBUS_SESSION_BUS_ADDRESS"] = bus_address
    return runtime_dir, bus_address, env


def _command_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = " ".join(str(completed.stderr or completed.stdout or "").split())
    return detail[:400] if detail else f"exit code {completed.returncode}"


__all__ = [
    "DEFAULT_RESTART_DELAY_SECONDS",
    "InstanceControl",
    "InstanceControlError",
    "InstanceServiceStatus",
    "RestartHandle",
    "cancel",
    "preflight_restart",
    "schedule_restart",
    "service_unit",
    "status",
    "validate_instance_id",
]
