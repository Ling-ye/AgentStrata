"""Code-health run inputs and host ports; no I/O or runtime dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, TypedDict

from chatcopilot.harness.models import RepairOptions


class TimeStop(TypedDict):
    mode: Literal["time"]
    seconds: int


class FindingsStop(TypedDict):
    mode: Literal["findings"]
    count: int


@dataclass(frozen=True)
class GovernanceOptions:
    model: str
    reasoning_effort: str = "medium"
    max_attempts: int = 3
    stop_condition: TimeStop | FindingsStop = field(default_factory=lambda: {"mode": "time", "seconds": 3600})

    def __post_init__(self):
        RepairOptions(self.model, self.reasoning_effort, self.max_attempts, None)
        stop = self.stop_condition
        key = {"time": "seconds", "findings": "count"}.get(stop.get("mode")) if isinstance(stop, dict) else None
        if key is None or set(stop) != {"mode", key} or type(stop[key]) is not int or stop[key] < 1:
            raise ValueError("停止条件必须为正整数执行秒数或问题发现数，且只能选择一种")

    @classmethod
    def from_payload(cls, value):
        if not isinstance(value, dict) or set(value) - {"model", "reasoning_effort", "max_attempts", "stop_condition"}:
            raise ValueError("代码熵回收配置包含无效或已移除的字段，请重新保存")
        if "model" not in value:
            raise ValueError("必须指定回收模型")
        return cls(**value)

    def to_payload(self):
        return asdict(self)


@dataclass(frozen=True)
class GovernanceSchedule:
    enabled: bool = False
    interval_hours: int = 24
    options: GovernanceOptions | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.interval_hours) is not int or self.interval_hours < 1:
            raise ValueError("定时开关必须为布尔值，间隔必须为正整数小时")
        if self.enabled and self.options is None:
            raise ValueError("启用定时代码熵回收必须明确模型与停止条件")

    @classmethod
    def from_payload(cls, value):
        allowed = {"enabled", "interval_hours", "options", "revision", "last_run", "last_error"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("旧熵回收定时配置不再支持，请重新保存设置")
        return cls(value.get("enabled", False), value.get("interval_hours", 24),
                   GovernanceOptions.from_payload(value["options"]) if value.get("options") else None)

    def to_payload(self):
        return asdict(self)


RUN_ACTIVE = frozenset({"running", "waiting_delivery", "cancel_requested"})


class GovernanceTaskPort(Protocol):
    def start(self, run: dict[str, Any], sequence: int, options: RepairOptions) -> dict[str, Any]: ...
    def launch(self, task_id: str) -> None: ...
    def resume(self, task_id: str) -> None: ...
    def retry_delivery(self, task_id: str) -> None: ...
