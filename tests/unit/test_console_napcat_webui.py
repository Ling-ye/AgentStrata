from __future__ import annotations

import subprocess
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.infra import router
from console.control import services
from chatcopilot.platforms.qq.gateway_health import OneBotRuntimeStatus


def _napcat() -> services.ServiceDef:
    service = services.find_service("napcat")
    assert service is not None
    return service


def _running_container() -> dict[str, object]:
    return {
        "running": True,
        "health": "none",
        "started_at": "2026-09-01T00:00:00Z",
    }


def test_napcat_status_reports_offline_account_separately_from_running_container() -> None:
    services._LOGIN_STATE_CACHE.clear()
    with (
        patch("console.control.services._docker_inspect", return_value=_running_container()),
        patch(
            "console.control.services._napcat_onebot_runtime_status",
            return_value=OneBotRuntimeStatus(online=False, good=True),
        ),
        patch("console.control.services._container_uptime_s", return_value=30),
    ):
        status = services.standalone_status(_napcat(), "lingye-copilot-qq")

    assert status["state"] == "unhealthy"
    assert status["color"] == "red"
    assert status["login_state"] == "logged_out"
    assert status["account_online"] is False
    assert status["provider_good"] is True
    assert any(check["name"] == "login" and not check["ok"] for check in status["checks"])


def test_napcat_status_promotes_online_healthy_account_to_healthy() -> None:
    services._LOGIN_STATE_CACHE.clear()
    with (
        patch("console.control.services._docker_inspect", return_value=_running_container()),
        patch(
            "console.control.services._napcat_onebot_runtime_status",
            return_value=OneBotRuntimeStatus(online=True, good=True),
        ),
        patch("console.control.services._container_uptime_s", return_value=30),
    ):
        status = services.standalone_status(_napcat(), "lingye-copilot-qq")

    assert status["state"] == "healthy"
    assert status["color"] == "green"
    assert status["login_state"] == "logged_in"
    assert status["account_online"] is True
    assert status["provider_good"] is True
    assert status["reasons"] == []


def test_napcat_status_exposes_unknown_when_onebot_status_cannot_be_read() -> None:
    services._LOGIN_STATE_CACHE.clear()
    with (
        patch("console.control.services._docker_inspect", return_value=_running_container()),
        patch(
            "console.control.services._napcat_onebot_runtime_status",
            side_effect=RuntimeError("provider unavailable"),
        ),
        patch("console.control.services._container_uptime_s", return_value=30),
    ):
        status = services.standalone_status(_napcat(), "lingye-copilot-qq")

    assert status["state"] == "running"
    assert status["color"] == "yellow"
    assert status["login_state"] is None
    assert status["account_online"] is None
    assert status["provider_good"] is None
    assert status["reasons"] == ["QQ account login state could not be verified."]


def test_explicit_login_check_prefers_live_onebot_status_over_webui_logs() -> None:
    with (
        patch(
            "console.control.services._napcat_onebot_runtime_status",
            return_value=OneBotRuntimeStatus(online=False, good=True),
        ),
        patch("console.control.services.read_webui_session") as webui_session,
    ):
        status = services.standalone_webui_login_status(
            _napcat(),
            "lingye-copilot-qq",
        )

    assert status == {
        "ok": True,
        "logged_in": False,
        "is_login": False,
        "is_offline": True,
        "provider_good": True,
        "login_error": "",
    }
    webui_session.assert_not_called()


def test_napcat_restart_delegates_to_prevalidated_provider_restart() -> None:
    with (
        patch(
            "console.control.services._napcat_provider_action",
            return_value={"ok": False, "error": "token missing"},
        ) as gateway,
        patch("console.control.services._docker_simple") as docker_simple,
    ):
        result = services.standalone_action(
            _napcat(),
            "lingye-copilot-qq",
            "restart",
        )

    assert result["ok"] is False
    gateway.assert_called_once_with("lingye-copilot-qq", "restart")
    docker_simple.assert_not_called()


def test_provider_action_strips_ansi_from_console_errors() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="\x1b[1;31m[ERR]\x1b[0m token missing",
    )
    with patch("console.control.services.subprocess.run", return_value=completed):
        result = services._napcat_provider_action("lingye-copilot-qq", "restart")

    assert result["ok"] is False
    assert result["stderr"] == "[ERR] token missing"


def test_console_does_not_expose_napcat_webui_token_routes() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get(
        "/api/infra/napcat:lingye-copilot-qq/webui-token"
    ).status_code == 404
    assert client.post(
        "/api/infra/napcat:lingye-copilot-qq/webui-session"
    ).status_code == 404


def test_napcat_login_check_route_uses_shared_webui_status() -> None:
    app = FastAPI()
    app.include_router(router)
    with patch(
        "console.backend.routes.infra.services.standalone_webui_login_status",
        return_value={
            "ok": True,
            "logged_in": False,
            "is_login": False,
            "is_offline": False,
            "login_error": "scan required",
        },
    ) as login_status:
        response = TestClient(app).post(
            "/api/infra/napcat:lingye-copilot-qq/login/check"
        )

    assert response.status_code == 200
    assert response.json()["logged_in"] is False
    login_status.assert_called_once_with(
        _napcat(),
        "lingye-copilot-qq",
        host="localhost",
        port="6099",
    )
