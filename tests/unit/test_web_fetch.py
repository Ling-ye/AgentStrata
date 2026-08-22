"""Unit tests for the ``web_fetch`` external tool."""
from __future__ import annotations

from email.message import Message
import urllib.error

import pytest

import chatcopilot.external_tools.web_fetch.tools as web_fetch_tools
from chatcopilot.agent.runtime import _hidden_by_search_entry
from chatcopilot.external_tools.web_fetch.tools import (
    TOOLS,
    _extract_text,
    _fetch_page,
    validate_url,
    web_fetch_page,
)


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str) -> None:
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _max_bytes: int) -> bytes:
        return self._body


class TestUrlValidation:
    def test_valid_https(self):
        assert validate_url("https://example.com/page") == "https://example.com/page"

    def test_valid_http(self):
        assert validate_url("http://example.com") == "http://example.com"

    def test_strips_whitespace(self):
        assert validate_url("  https://example.com  ") == "https://example.com"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            validate_url("")

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_url("file:///etc/passwd")

    def test_rejects_ftp_scheme(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_url("ftp://mirror.example.com/file")

    def test_rejects_data_scheme(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_url("data:text/html,<h1>hi</h1>")

    def test_rejects_no_hostname(self):
        with pytest.raises(ValueError, match="no hostname"):
            validate_url("https://")

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(ValueError, match="unsupported"):
            validate_url("gopher://example.com")

    @pytest.mark.parametrize(
        "url",
        (
            "http://127.0.0.1:8910/health",
            "http://localhost:3001/",
            "https://" + ".".join(("10", "0", "0", "1")) + "/api",
            "https://" + ".".join(("192", "168", "1", "1")) + "/",
        ),
    )
    def test_accepts_private_and_loopback_http_destinations(self, url):
        assert validate_url(url) == url


class TestHtmlExtraction:
    def test_extracts_title(self):
        html = "<html><head><title>Hello World</title></head><body><p>content</p></body></html>"
        title, text = _extract_text(html)
        assert title == "Hello World"
        assert "content" in text

    def test_strips_script_tags(self):
        html = "<p>before</p><script>alert('xss')</script><p>after</p>"
        _, text = _extract_text(html)
        assert "alert" not in text
        assert "before" in text
        assert "after" in text

    def test_strips_style_tags(self):
        html = "<style>.foo{color:red}</style><p>visible</p>"
        _, text = _extract_text(html)
        assert "color" not in text
        assert "visible" in text

    def test_adds_newlines_for_block_elements(self):
        html = "<p>para1</p><p>para2</p>"
        _, text = _extract_text(html)
        assert "para1" in text
        assert "para2" in text
        lines = text.strip().split("\n")
        assert len(lines) >= 2

    def test_handles_malformed_html(self):
        html = "<p>unclosed <b>bold <i>italic"
        _, text = _extract_text(html)
        assert "unclosed" in text


class TestToolDef:
    def test_tool_def_exists(self):
        assert web_fetch_page is not None
        assert web_fetch_page.name == "web_fetch_page"
        assert web_fetch_page.category == "web_fetch"
        assert web_fetch_page.owner == "web_fetch"
        assert web_fetch_page.requires_role == "owner"

    def test_tools_list_exported(self):
        assert len(TOOLS) == 1
        assert TOOLS[0] is web_fetch_page

    def test_unified_search_hides_direct_fetch(self):
        assert _hidden_by_search_entry(web_fetch_page) is True

    def test_required_fields(self):
        assert "url" in web_fetch_page.required

    def test_properties_schema(self):
        assert "url" in web_fetch_page.properties
        assert "max_chars" in web_fetch_page.properties
        assert web_fetch_page.properties["url"]["type"] == "string"
        assert web_fetch_page.properties["max_chars"]["type"] == "integer"
        assert (
            web_fetch_page.properties["max_chars"]["description"]
            == "Maximum characters of page text to return (100-50000)."
        )


class TestHandler:
    def test_empty_url_returns_error(self):
        result, outputs, hint = web_fetch_page.handler({"url": ""})
        assert result == "Error: Missing required parameter: url."
        assert outputs == []
        assert hint is None

    def test_invalid_max_chars_returns_readable_error(self):
        result, outputs, hint = web_fetch_page.handler(
            {"url": "https://example.com", "max_chars": "many"}
        )
        assert result == "Error: Invalid max_chars: expected an integer."
        assert outputs == []
        assert hint is None

    def test_max_chars_is_clamped_low(self, monkeypatch):
        captured = {}

        def fake_fetch(url, max_chars):
            captured["url"] = url
            captured["max_chars"] = max_chars
            return "ok"

        monkeypatch.setattr(web_fetch_tools, "_fetch_page", fake_fetch)

        result, outputs, hint = web_fetch_page.handler(
            {"url": "https://example.com", "max_chars": 10}
        )

        assert result == "ok"
        assert captured == {"url": "https://example.com", "max_chars": 100}
        assert outputs == []
        assert hint is None


class TestFetchOutput:
    def test_success_output_has_consistent_fields(self, monkeypatch):
        html = b"<html><head><title>Example</title></head><body><p>Hello</p></body></html>"
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)
        monkeypatch.setattr(
            web_fetch_tools.urllib.request,
            "urlopen",
            lambda *args, **kwargs: _FakeResponse(html, "text/html; charset=utf-8"),
        )

        result = _fetch_page("https://example.com", 8000)

        assert result == (
            "Title: Example\n"
            "URL: https://example.com\n"
            "Content:\n"
            "Hello"
        )

    def test_missing_title_uses_placeholder(self, monkeypatch):
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)
        monkeypatch.setattr(
            web_fetch_tools.urllib.request,
            "urlopen",
            lambda *args, **kwargs: _FakeResponse(b'{"ok": true}', "application/json"),
        )

        result = _fetch_page("https://example.com/data", 8000)

        assert result.startswith(
            "Title: (not available)\n"
            "URL: https://example.com/data\n"
            "Content:\n"
        )

    def test_truncation_is_reported_separately(self, monkeypatch):
        html = f"<html><body><p>{'x' * 120}</p></body></html>".encode()
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)
        monkeypatch.setattr(
            web_fetch_tools.urllib.request,
            "urlopen",
            lambda *args, **kwargs: _FakeResponse(html, "text/html; charset=utf-8"),
        )

        result = _fetch_page("https://example.com", 100)

        assert "Content:\n" + ("x" * 100) in result
        assert "Truncated: showing the first 100 of 120 characters." in result


class TestFetchErrors:
    def test_invalid_url_returns_readable_error(self, monkeypatch):
        def reject_url(_url):
            raise ValueError("unsupported scheme 'ftp'; only http/https")

        monkeypatch.setattr(web_fetch_tools, "validate_url", reject_url)

        result = _fetch_page("ftp://example.com", 8000)

        assert result == (
            "Error: Invalid URL: unsupported scheme 'ftp'; only http/https"
        )

    def test_http_error_is_concise(self, monkeypatch):
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)

        def raise_http_error(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://example.com/missing", 404, "Not Found", {}, None
            )

        monkeypatch.setattr(
            web_fetch_tools.urllib.request, "urlopen", raise_http_error
        )

        result = _fetch_page("https://example.com/missing", 8000)

        assert result == "Error: HTTP 404 Not Found."

    def test_network_error_is_readable(self, monkeypatch):
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)

        def raise_url_error(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(
            web_fetch_tools.urllib.request, "urlopen", raise_url_error
        )

        result = _fetch_page("https://example.com", 8000)

        assert result == "Error: Network request failed: connection refused"

    def test_timeout_is_readable(self, monkeypatch):
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)

        def raise_timeout(*args, **kwargs):
            raise TimeoutError

        monkeypatch.setattr(web_fetch_tools.urllib.request, "urlopen", raise_timeout)

        result = _fetch_page("https://example.com", 8000)

        assert result == "Error: Request timed out after 15 seconds."

    def test_unsupported_content_type_is_readable(self, monkeypatch):
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)
        monkeypatch.setattr(
            web_fetch_tools.urllib.request,
            "urlopen",
            lambda *args, **kwargs: _FakeResponse(b"%PDF", "application/pdf"),
        )

        result = _fetch_page("https://example.com/file.pdf", 8000)

        assert result == (
            "Error: Unsupported content type: application/pdf. "
            "Only text, HTML, and JSON responses are supported."
        )

    def test_unknown_error_is_readable(self, monkeypatch):
        monkeypatch.setattr(web_fetch_tools, "validate_url", lambda url: url)

        def raise_unknown(*args, **kwargs):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr(web_fetch_tools.urllib.request, "urlopen", raise_unknown)

        result = _fetch_page("https://example.com", 8000)

        assert result == "Error: Failed to fetch page: unexpected failure"
