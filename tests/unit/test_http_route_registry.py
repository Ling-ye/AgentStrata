from __future__ import annotations

import sys
import types
from http import HTTPStatus

import pytest

from chatcopilot.middleware.http.auth import HttpError
from chatcopilot.middleware.http.registry import dispatch_request


def test_http_route_dispatch_uses_registered_module(monkeypatch) -> None:
    module = types.ModuleType("tests.fake_http_route")

    def handle_request(method, path, headers, payload):
        assert method == "POST"
        assert path == "/fake"
        return HTTPStatus.ACCEPTED, {"ok": True, "payload": payload}

    module.handle_request = handle_request
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("CHATCOPILOT_HTTP_ROUTE_MODULES", module.__name__)

    status, body = dispatch_request("POST", "/fake", {}, {"x": 1})

    assert status == HTTPStatus.ACCEPTED
    assert body == {"ok": True, "payload": {"x": 1}}


def test_http_route_dispatch_defaults_to_no_business_routes(monkeypatch) -> None:
    monkeypatch.delenv("CHATCOPILOT_HTTP_ROUTE_MODULES", raising=False)

    status, body = dispatch_request("GET", "/healthz", {}, {})

    assert status == HTTPStatus.OK
    assert body["routes"] == ()
    with pytest.raises(HttpError) as exc:
        dispatch_request("GET", "/missing/healthz", {}, {})
    assert exc.value.status == HTTPStatus.NOT_FOUND
