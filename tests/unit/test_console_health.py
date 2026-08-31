from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from console.control import health, operations
from console.control.instances import BotInstance


class _EmptyTasks:
    def list(self) -> list[dict[str, object]]:
        return []


def _inst(*, log_dir: str = "/tmp/chatcopilot-logs/sample-bot") -> BotInstance:
    return BotInstance(
        instance_id="sample-bot",
        bot_spec="bots/sample-bot/bot.yaml",
        display_name="SampleBot",
        platform="qq",
        runtime_kind="gateway",
        wsl_home="/tmp/ChatCopilot-sample-bot",
        workspace_root="/tmp/chatcopilot-workspace",
        log_dir=log_dir,
        env_file="/tmp/.chatcopilot-sample-bot.env",
        project_name="chatcopilot-sample-bot",
    )


def _gateway_status(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "is_deployed": True,
        "registered": True,
        "running": True,
        "runtime_kind": "gateway",
        "main_process_verified": True,
        "channel_connected": True,
        "runtime_log": "/tmp/gateway.log",
        "runtime_log_age_s": 900,
        "runtime_log_size": 1024,
        "error_count": 0,
    }
    value.update(overrides)
    return value


def test_status_checks_keep_stale_gateway_log_informational() -> None:
    checks, reasons = operations._status_checks(_gateway_status())

    runtime_logs = next(item for item in checks if item["name"] == "runtime_logs")
    assert runtime_logs["ok"] is True
    assert runtime_logs["severity"] == "info"
    assert "Last updated 900s ago" in str(runtime_logs["message"])
    assert reasons == []


def test_log_signal_reports_gateway_channel_evidence(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    gateway_log = log_dir / "gateway" / "current.log"
    gateway_log.parent.mkdir(parents=True)
    gateway_log.write_text(
        "onebot.connected\n[ERR] provider connection closed\n",
        encoding="utf-8",
    )

    signal = operations._log_signal(_inst(log_dir=str(log_dir)))

    assert signal["runtime_log"] == str(gateway_log)
    assert signal["channel_connected"] is True
    assert signal["error_count"] == 1
    assert "provider connection closed" in str(signal["error_summary"])


def test_status_checks_report_channel_failure_as_critical() -> None:
    checks, reasons = operations._status_checks(
        _gateway_status(channel_connected=False)
    )

    channel = next(item for item in checks if item["name"] == "channel_connection")
    assert channel["ok"] is False
    assert channel["severity"] == "critical"
    assert any("Channel" in reason for reason in reasons)


def test_status_checks_reject_unverified_gateway_mainpid() -> None:
    checks, reasons = operations._status_checks(
        _gateway_status(main_process_verified=False, running=False)
    )

    gateway = next(item for item in checks if item["name"] == "gateway_main_process")
    assert gateway["ok"] is False
    assert gateway["severity"] == "critical"
    assert any("exact instance Gateway" in reason for reason in reasons)


def test_overview_does_not_mark_bot_unhealthy_for_stale_gateway_log() -> None:
    inst = _inst()
    status = {
        **_gateway_status(),
        "instance_id": inst.instance_id,
        "display_name": inst.display_name,
        "platform": inst.platform,
        "active_state": "active",
        "sub_state": "running",
    }
    status["checks"], status["reasons"] = operations._status_checks(status)

    with (
        patch("console.control.operations.status", return_value=status),
        patch("console.control.services.all_services_status", return_value=[]),
        patch("console.control.health._recent_workspace_failures", return_value=[]),
    ):
        overview = health.overview([inst], _EmptyTasks())

    assert overview["summary"]["bots_unhealthy"] == 0
    assert overview["summary"]["issues_warning"] == 0
    assert overview["bots"][0]["health_color"] == "green"
    assert overview["issues"] == []


def test_overview_reports_channel_connection_failure() -> None:
    inst = _inst()
    status = {
        **_gateway_status(channel_connected=False),
        "instance_id": inst.instance_id,
        "display_name": inst.display_name,
        "platform": inst.platform,
        "active_state": "active",
        "sub_state": "running",
    }
    status["checks"], status["reasons"] = operations._status_checks(status)

    with (
        patch("console.control.operations.status", return_value=status),
        patch("console.control.services.all_services_status", return_value=[]),
        patch("console.control.health._recent_workspace_failures", return_value=[]),
    ):
        overview = health.overview([inst], _EmptyTasks())

    assert overview["summary"]["bots_unhealthy"] == 1
    assert overview["summary"]["issues_critical"] == 1
    assert overview["bots"][0]["health_color"] == "red"
    assert overview["issues"][0]["title"] == "configured Channel is not connected."
