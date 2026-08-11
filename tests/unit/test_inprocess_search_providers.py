from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from chatcopilot.agent.search import providers
from chatcopilot.agent.search.providers import (
    DirectSearchProvider,
    HttpSearchProviderClient,
    SearchProviderError,
    SearchProviderRegistry,
)
from chatcopilot.contracts.subagents import SearchProviderSpec


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def _spec(kind: str, **overrides: object) -> SearchProviderSpec:
    values: dict[str, object] = {
        "id": kind,
        "kind": kind,
        "enabled": True,
        "endpoint": None,
        "credential_env": None,
        "timeout_seconds": 15.0,
        "max_results": 10,
    }
    values.update(overrides)
    return SearchProviderSpec(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (
            urllib.error.HTTPError("https://example.invalid", 401, "", None, None),
            "search_authentication_failed",
        ),
        (
            urllib.error.HTTPError("https://example.invalid", 429, "", None, None),
            "search_quota_exceeded",
        ),
        (
            urllib.error.HTTPError("https://example.invalid", 432, "", None, None),
            "search_quota_exceeded",
        ),
        (
            urllib.error.HTTPError("https://example.invalid", 433, "", None, None),
            "search_quota_exceeded",
        ),
        (socket.timeout(), "search_timeout"),
        (urllib.error.URLError("offline"), "search_transport_error"),
    ],
)
def test_provider_failures_map_to_stable_non_secret_codes(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    error_code: str,
) -> None:
    client = HttpSearchProviderClient(_spec("tavily"), "tvly-never-print-this")

    def fail(*_args: object, **_kwargs: object) -> dict:
        raise failure

    monkeypatch.setattr(providers, "_request_json", fail)

    with pytest.raises(SearchProviderError) as caught:
        client.search("query")

    assert caught.value.error_code == error_code
    assert str(caught.value) == error_code
    assert "tvly-never-print-this" not in str(caught.value)


def test_provider_rejects_invalid_success_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "_request_json", lambda *_args, **_kwargs: {"ok": True})

    with pytest.raises(SearchProviderError, match="search_invalid_response"):
        HttpSearchProviderClient(_spec("tavily"), "secret").search("query")


def test_searxng_loopback_disables_proxy_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: list[object] = []

    class _Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> _Response:
            assert request.full_url.startswith("http://127.0.0.1:18064/search?")
            assert timeout == 15.0
            return _Response(
                {"results": [{"title": "AgentStrata", "url": "https://example.com"}]}
            )

    def build_opener(*handlers: object) -> _Opener:
        captured_handlers.extend(handlers)
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)

    result = HttpSearchProviderClient(_spec("searxng")).search("AgentStrata")

    assert len(result["results"]) == 1
    assert any(
        isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {}
        for handler in captured_handlers
    )
    assert any(
        isinstance(handler, providers._NoRedirectHandler)
        for handler in captured_handlers
    )


def test_credentialed_provider_redirect_handler_rejects_redirect() -> None:
    handler = providers._NoRedirectHandler()
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        headers={"Authorization": "Bearer secret"},
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_client_refuses_malicious_credential_endpoint_before_network() -> None:
    client = HttpSearchProviderClient(
        _spec(
            "tavily",
            endpoint="https://api.tavily.com.attacker.example/search",
        ),
        "tvly-never-send-this",
    )

    with pytest.raises(SearchProviderError, match="search_invalid_configuration"):
        client.search("query")


def test_direct_provider_falls_back_in_declared_order_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-never-print-this")
    specs = (
        _spec("tavily", credential_env="TAVILY_API_KEY"),
        _spec("searxng"),
    )

    def request_json(request: urllib.request.Request, **_kwargs: object) -> dict:
        if request.full_url == "https://api.tavily.com/search":
            assert request.get_header("Authorization") == "Bearer tvly-never-print-this"
            assert b"tvly-never-print-this" not in (request.data or b"")
            raise urllib.error.HTTPError(request.full_url, 432, "quota", None, None)
        return {
            "results": [
                {
                    "title": "AgentStrata search architecture",
                    "url": "https://example.com/agentstrata",
                    "content": "AgentStrata search provider design",
                }
            ]
        }

    monkeypatch.setattr(providers, "_request_json", request_json)
    registry = SearchProviderRegistry.from_tools((), provider_specs=specs)

    result = DirectSearchProvider(registry=registry).search(
        logical_source="web",
        query="AgentStrata search",
    )

    assert result is not None
    assert result["ok"] is True
    assert result["actual_source"] == "searxng"
    assert result["summary"]["provider_attempts"] == [
        {"server": "tavily", "status": "failed", "reason": "search_quota_exceeded"}
    ]
    assert "tvly-never-print-this" not in json.dumps(result)


def test_missing_credential_is_skipped_without_hiding_available_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        providers,
        "_request_json",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "title": "AgentStrata",
                    "url": "https://example.com/agentstrata",
                    "content": "AgentStrata query",
                }
            ]
        },
    )
    registry = SearchProviderRegistry.from_tools(
        (),
        provider_specs=(
            _spec("tavily", credential_env="TAVILY_API_KEY"),
            _spec("searxng"),
        ),
    )

    result = DirectSearchProvider(registry=registry).search(
        logical_source="web",
        query="AgentStrata",
    )

    assert result is not None
    assert result["ok"] is True
    assert result["summary"]["provider_attempts"] == [
        {
            "server": "tavily",
            "status": "unavailable",
            "reason": "search_credential_missing",
        }
    ]
