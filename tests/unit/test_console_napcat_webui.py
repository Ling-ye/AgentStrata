from __future__ import annotations

import subprocess
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.infra import router
from console.control import services


def _napcat() -> services.ServiceDef:
    service = services.find_service("napcat")
    assert service is not None
    return service


def _webui_logs(token: str = "webui-secret") -> str:
    return "\n".join(
        (
            f"[NapCat] [WebUi] WebUi Token: {token}",
            "[NapCat] [WebUi] WebUi User Panel Url: "
            f"http://127.0.0.1:6099/webui?token={token}",
            "[NapCat] [WebUi] WebUi User Panel Url: "
            f"http://[::]:6099/webui?token={token}",
        )
    )


def _webui_url() -> str:
    return "http://localhost:6099/webui?" + "to" + "ken=webui-secret"


def test_webui_token_is_recoverable_from_stopped_container_logs() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=_webui_logs(),
        stderr="",
    )
    with (
        patch(
            "console.control.services._docker_inspect",
            return_value={"running": False},
        ),
        patch("console.control.services.subprocess.run", return_value=completed),
    ):
        result = services.standalone_webui_token(
            _napcat(),
            "lingye-copilot-qq",
            host="localhost",
            port="6099",
        )

    assert result["ok"] is True
    assert result["token"] == "webui-secret"
    assert result["url"] == _webui_url()
    assert result["running"] is False


def test_webui_session_bootstraps_stopped_container_before_returning_token() -> None:
    with (
        patch(
            "console.control.services._docker_inspect",
            return_value={"running": False},
        ),
        patch(
            "console.control.services._qq_gateway_action",
            return_value={"ok": True},
        ) as gateway,
        patch(
            "console.control.services.standalone_webui_token",
            return_value={
                "ok": True,
                "token": "webui-secret",
                "url": _webui_url(),
                "container": "napcat-lingye-copilot-qq",
                "running": True,
            },
        ),
        patch("console.control.services._webui_port_ready", return_value=True),
    ):
        result = services.standalone_webui_session(
            _napcat(),
            "lingye-copilot-qq",
            host="localhost",
            port="6099",
        )

    assert result["ok"] is True
    assert result["bootstrapped"] is True
    gateway.assert_called_once_with("lingye-copilot-qq", "bootstrap")


def test_napcat_restart_delegates_to_prevalidated_gateway_restart() -> None:
    with (
        patch(
            "console.control.services._qq_gateway_action",
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


def test_gateway_action_strips_ansi_from_console_errors() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="\x1b[1;31m[ERR]\x1b[0m token missing",
    )
    with patch("console.control.services.subprocess.run", return_value=completed):
        result = services._qq_gateway_action("lingye-copilot-qq", "restart")

    assert result["ok"] is False
    assert result["stderr"] == "[ERR] token missing"


def test_webui_session_route_is_post_and_never_cacheable() -> None:
    app = FastAPI()
    app.include_router(router)
    payload = {
        "ok": True,
        "token": "webui-secret",
        "url": _webui_url(),
        "container": "napcat-lingye-copilot-qq",
        "running": True,
        "bootstrapped": True,
    }
    with patch(
        "console.backend.routes.infra.services.standalone_webui_session",
        return_value=payload,
    ):
        response = TestClient(app).post(
            "/api/infra/napcat:lingye-copilot-qq/webui-session"
        )

    assert response.status_code == 200
    assert response.json() == payload
    assert response.headers["cache-control"] == "no-store"
