from __future__ import annotations

import pytest

from chatcopilot.__main__ import main
from chatcopilot.middleware.http.routes import handle_request
from chatcopilot.project import format_legal_info, legal_info, source_command_reply


def test_missing_deployment_source_is_explicit(monkeypatch, capsys):
    monkeypatch.delenv("CHATCOPILOT_SOURCE_URL", raising=False)
    info = legal_info()
    assert info["source_configured"] is False
    assert info["source_url"] == info["repository_url"]
    assert info["license"] == "AGPL-3.0-only"
    assert "Copyright (C) 2026 Lingye" in format_legal_info()
    assert "尚未设置本部署对应源码" in format_legal_info()
    assert main(["source"]) == 0
    assert "尚未设置本部署对应源码" in capsys.readouterr().out
    assert main(["source", "--check"]) == 2


def test_all_read_only_entrypoints_share_configured_source(monkeypatch, capsys):
    source = "https://example.org/project/source.tar.gz?sha=abc"
    monkeypatch.setenv("CHATCOPILOT_SOURCE_URL", source)
    info = legal_info()
    assert info["source_configured"] is True
    assert info["source_url"] == source
    assert source_command_reply(" /SOURCE ") == format_legal_info()
    assert main(["license"]) == 0
    assert source in capsys.readouterr().out
    assert main(["source", "--check"]) == 0
    status, payload = handle_request("GET", "/legal", {}, {})
    assert int(status) == 200
    assert payload == {"ok": True, **info}


@pytest.mark.parametrize("source", ["file:///tmp/source", "javascript:alert(1)", "https://" + "user:password" + "@example.org/source", "https://example.org/source\nheader"])
def test_source_configuration_cannot_inject_non_web_links_or_credentials(monkeypatch, source):
    monkeypatch.setenv("CHATCOPILOT_SOURCE_URL", source)
    with pytest.raises(ValueError, match="SOURCE_URL"):
        legal_info()


def test_source_arguments_and_regular_text():
    assert source_command_reply("/source verbose") == "用法：/source"
    assert source_command_reply("please /source") is None
    assert source_command_reply("/source/path") is None


def test_console_legal_route_exposes_only_public_info(monkeypatch):
    from fastapi.testclient import TestClient
    from console.backend.app import app

    monkeypatch.delenv("CHATCOPILOT_SOURCE_URL", raising=False)
    response = TestClient(app).get("/api/legal")
    assert response.status_code == 200
    assert response.json() == legal_info()
    assert response.headers["Cache-Control"] == "no-store"
