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
