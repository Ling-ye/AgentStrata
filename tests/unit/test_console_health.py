from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from unittest.mock import patch

from console.control import health, operations
from console.control.instances import BotInstance

_TMP_PARENT = Path(__file__).resolve().parents[2] / "scratch_unit_tests" / "console-health"


class _EmptyTasks:
    def list(self) -> list[dict[str, object]]:
        return []


def _inst() -> BotInstance:
    return BotInstance(
        instance_id="sample-bot",
        bot_spec="bots/sample-bot/bot.yaml",
        display_name="SampleBot",
        platform="feishu",
        wsl_home="/tmp/ChatCopilot-sample-bot",
        workspace_root="/tmp/chatcopilot-workspace",
        log_dir="/tmp/chatcopilot-logs/sample-bot",
        env_file="/tmp/.chatcopilot-sample-bot.env",
        cc_connect_config_dir="/tmp/.chatcopilot-runtime/sample-bot/.cc-connect",
        cc_home="/tmp/.chatcopilot-runtime/sample-bot",
        project_name="chatcopilot-sample-bot",
    )


def test_status_checks_keep_stale_cc_log_informational() -> None:
    status = {
        "is_deployed": True,
        "registered": True,
        "running": True,
        "ws_connected": True,
        "cc_log": "/tmp/cc-connect.log",
        "cc_log_age_s": 900,
        "cc_log_size": 1024,
        "error_count": 0,
    }

    checks, reasons = operations._status_checks(status)

    fresh = next(item for item in checks if item["name"] == "fresh_logs")
    assert fresh["ok"] is True
    assert fresh["severity"] == "info"
    assert "Last updated 900s ago" in str(fresh["message"])
    assert reasons == []


def test_log_signal_reports_qq_proxy_upstream_failure() -> None:
    log_dir = _TMP_PARENT / "logs" / "lingye-copilot-qq"
    shutil.rmtree(_TMP_PARENT, ignore_errors=True)
    cc_dir = log_dir / "cc-connect"
    proxy_dir = log_dir / "qq-at-proxy"
    cc_dir.mkdir(parents=True)
    proxy_dir.mkdir(parents=True)
    today = date.today().isoformat()
    (cc_dir / f"{today}.log").write_text(
        "\n".join(
            [
                'time=2026-06-26T15:42:13+08:00 level=INFO msg="qq: reconnected"',
                'time=2026-06-26T15:42:13+08:00 level=ERROR msg="qq: ws read error, reconnecting..." error="websocket: close 1000 (normal)"',
            ]
        ),
        encoding="utf-8",
    )
    (proxy_dir / f"{today}.log").write_text(
        '[2026-06-26 15:43:37] ERROR chatcopilot.platforms.qq.at_proxy | upstream connect failed (ws://127.0.0.1:3001): did not receive a valid HTTP response\n',
        encoding="utf-8",
    )
    inst = BotInstance(
        instance_id="lingye-copilot-qq",
        bot_spec="bots/lingye-copilot-qq/bot.yaml",
        display_name="Lingye",
        platform="qq",
        log_dir=str(log_dir),
    )

    try:
        signal = operations._log_signal(inst)

        assert signal["ws_connected"] is False
        assert signal["error_count"] == 1
        assert "websocket is closing immediately" in str(signal["error_summary"])
        assert signal["qq_proxy_error_count"] == 1
        assert "cannot reach NapCat" in str(signal["qq_proxy_error_summary"])
    finally:
        shutil.rmtree(_TMP_PARENT, ignore_errors=True)


def test_status_checks_report_qq_proxy_failure_as_critical() -> None:
    status = {
        "is_deployed": True,
        "registered": True,
        "running": True,
        "ws_connected": False,
        "error_count": 1,
        "error_summary": "QQ OneBot websocket is closing immediately.",
        "qq_proxy_error_count": 1,
        "qq_proxy_error_summary": "QQ @ proxy cannot reach NapCat OneBot upstream.",
    }

    checks, reasons = operations._status_checks(status)

    proxy = next(item for item in checks if item["name"] == "qq_proxy")
    assert proxy["ok"] is False
    assert proxy["severity"] == "critical"
    assert "cannot reach NapCat" in proxy["message"]
    assert any("QQ @ proxy" in reason for reason in reasons)


def test_overview_does_not_mark_bot_unhealthy_for_stale_cc_log() -> None:
    inst = _inst()
    status = {
        "instance_id": inst.instance_id,
        "display_name": inst.display_name,
        "platform": inst.platform,
        "is_deployed": True,
        "registered": True,
        "running": True,
        "active_state": "active",
        "sub_state": "running",
        "ws_connected": True,
        "cc_log": "/tmp/cc-connect.log",
        "cc_log_age_s": 900,
        "cc_log_size": 1024,
        "error_count": 0,
    }

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


def test_overview_still_reports_platform_connection_failure() -> None:
    inst = _inst()
    status = {
        "instance_id": inst.instance_id,
        "display_name": inst.display_name,
        "platform": inst.platform,
        "is_deployed": True,
        "registered": True,
        "running": True,
        "active_state": "active",
        "sub_state": "running",
        "ws_connected": False,
        "cc_log": "/tmp/cc-connect.log",
        "cc_log_age_s": 900,
        "cc_log_size": 1024,
        "error_count": 0,
    }

    with (
        patch("console.control.operations.status", return_value=status),
        patch("console.control.services.all_services_status", return_value=[]),
        patch("console.control.health._recent_workspace_failures", return_value=[]),
    ):
        overview = health.overview([inst], _EmptyTasks())

    assert overview["summary"]["bots_unhealthy"] == 1
    assert overview["summary"]["issues_critical"] == 1
    assert overview["bots"][0]["health_color"] == "red"
    assert overview["issues"][0]["title"] == "platform websocket is not connected."
