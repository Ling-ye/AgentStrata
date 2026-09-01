from __future__ import annotations

import subprocess
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.infra import router
from console.control import services
from chatcopilot.platforms.qq.gateway_health import OneBotRuntimeStatus
from chatcopilot.platforms.qq.webui_session import QQLoginStatus, WebUiSession


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
        patch(
            "console.control.services._napcat_webui_entrypoint",
            return_value="http://localhost:16099/webui",
        ),
        patch("console.control.services._container_uptime_s", return_value=30),
    ):
        status = services.standalone_status(_napcat(), "lingye-copilot-qq")

    assert status["state"] == "unhealthy"
    assert status["color"] == "red"
    assert status["login_state"] == "logged_out"
    assert status["account_online"] is False
    assert status["provider_good"] is True
    assert status["login_url"] == "http://localhost:16099/webui"
    assert "?" not in status["login_url"]
    assert any(check["name"] == "login" and not check["ok"] for check in status["checks"])


def test_napcat_status_promotes_online_healthy_account_to_healthy() -> None:
    services._LOGIN_STATE_CACHE.clear()
    with (
        patch("console.control.services._docker_inspect", return_value=_running_container()),
        patch(
            "console.control.services._napcat_onebot_runtime_status",
            return_value=OneBotRuntimeStatus(online=True, good=True),
        ),
        patch(
            "console.control.services._napcat_webui_entrypoint",
            return_value="http://localhost:6099/webui",
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
        patch(
            "console.control.services._napcat_webui_entrypoint",
            return_value="http://localhost:6099/webui",
        ),
        patch("console.control.services._container_uptime_s", return_value=30),
    ):
        status = services.standalone_status(_napcat(), "lingye-copilot-qq")

    assert status["state"] == "running"
    assert status["color"] == "yellow"
    assert status["login_state"] is None
    assert status["account_online"] is None
    assert status["provider_good"] is None
    assert status["login_url"] == "http://localhost:6099/webui"
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


def test_napcat_webui_entrypoint_uses_instance_local_port_without_token() -> None:
    with patch(
        "console.control.services._napcat_local_env",
        return_value={"QQ_WEBUI_PORT": "16099", "QQ_ACCESS_TOKEN": "never-return"},
    ):
        url = services._napcat_webui_entrypoint("lingye-copilot-qq")

    assert url == "http://localhost:16099/webui"
    assert "never-return" not in url


def test_napcat_webui_token_reads_the_bounded_instance_session_on_demand() -> None:
    token = "-".join(("temporary", "webui", "value"))
    session = WebUiSession(
        container="napcat-lingye-copilot-qq",
        host="localhost",
        port=16099,
        token=token,
        url="http://localhost:16099/webui",
        running=True,
    )
    with (
        patch("console.control.services._napcat_webui_port", return_value="16099"),
        patch(
            "console.control.services.read_webui_session",
            return_value=session,
        ) as read_session,
    ):
        result = services.standalone_webui_token(
            _napcat(),
            "lingye-copilot-qq",
        )

    assert result == {"ok": True, "token": token}
    read_session.assert_called_once_with(
        "napcat-lingye-copilot-qq",
        host="localhost",
        port="16099",
    )


def test_napcat_webui_token_rejects_invalid_instance_before_reading_logs() -> None:
    with patch("console.control.services.read_webui_session") as read_session:
        result = services.standalone_webui_token(_napcat(), "invalid/instance")

    assert result == {"ok": False, "error": "invalid bot instance id"}
    read_session.assert_not_called()


def test_webui_login_fallback_uses_the_same_instance_local_port() -> None:
    token_key = "to" + "ken"
    session = WebUiSession(
        container="napcat-lingye-copilot-qq",
        host="localhost",
        port=16099,
        token="private-token",
        url=f"http://localhost:16099/webui?{token_key}=private-token",
        running=True,
    )
    with (
        patch(
            "console.control.services._napcat_onebot_runtime_status",
            side_effect=RuntimeError("provider unavailable"),
        ),
        patch(
            "console.control.services._napcat_webui_port",
            return_value="16099",
        ),
        patch(
            "console.control.services.read_webui_session",
            return_value=session,
        ) as read_session,
        patch(
            "console.control.services.check_napcat_login_status",
            return_value=QQLoginStatus(False, True, "scan required", True),
        ),
    ):
        status = services.standalone_webui_login_status(
            _napcat(),
            "lingye-copilot-qq",
        )

    assert status["logged_in"] is False
    read_session.assert_called_once_with(
        "napcat-lingye-copilot-qq",
        host="localhost",
        port="16099",
    )


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


def test_deprecated_napcat_webui_token_routes_remain_unavailable() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get(
        "/api/infra/napcat:lingye-copilot-qq/webui-token"
    ).status_code == 404
    assert client.post(
        "/api/infra/napcat:lingye-copilot-qq/webui-session"
    ).status_code == 404


def test_napcat_token_route_is_loopback_only_and_never_cached() -> None:
    app = FastAPI()
    app.include_router(router)
    token = "-".join(("temporary", "webui", "value"))
    with patch(
        "console.backend.routes.infra.services.standalone_webui_token",
        return_value={"ok": True, "token": token},
    ) as token_lookup:
        response = TestClient(
            app,
            client=("127.0.0.1", 50000),
        ).post("/api/infra/napcat:lingye-copilot-qq/login/token")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "token": token}
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    token_lookup.assert_called_once_with(_napcat(), "lingye-copilot-qq")


def test_napcat_token_route_rejects_nonloopback_before_reading_logs() -> None:
    app = FastAPI()
    app.include_router(router)
    remote_host = "192.0." + "2.10"
    with patch(
        "console.backend.routes.infra.services.standalone_webui_token",
    ) as token_lookup:
        response = TestClient(
            app,
            client=(remote_host, 50000),
        ).post(
            "/api/infra/napcat:lingye-copilot-qq/login/token",
            headers={
                "Host": "localhost:8910",
                "Origin": "http://localhost:8910",
                "X-Forwarded-For": "127.0.0.1",
            },
        )

    assert response.status_code == 403
    assert "loopback" in response.json()["detail"]
    token_lookup.assert_not_called()


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
    )
