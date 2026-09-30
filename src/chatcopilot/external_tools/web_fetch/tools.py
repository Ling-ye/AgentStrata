"""``web_fetch`` tool: fetch a public URL and extract readable text content.

Zero external dependencies: uses only the standard library
(``urllib`` + ``html.parser``). Designed as a fallback when MCP-based
extractors such as ``tavily_extract`` are unavailable.
"""
from __future__ import annotations

import html.parser
from dataclasses import dataclass
import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List
from urllib.parse import urlparse

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.external_tools.shared.tool_spec import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)

_CATEGORY = "web_fetch"
_OWNER = "web_fetch"
_TIMEOUT_SECONDS = 60
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
_DEFAULT_MAX_CHARS = 8000
_USER_AGENT = "AgentStrata/0.1 (+https://github.com/Ling-ye/AgentStrata; web_fetch_page)"

_BLOCKED_SCHEMES = {"file", "ftp", "data", "javascript"}
_STRIP_TAGS = {"script", "style", "noscript", "svg", "head", "iframe", "object", "embed"}


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def validate_url(url: str) -> str:
    """Validate and normalize a URL, raising ``ValueError`` when disallowed."""
    url = url.strip()
    if not url:
        raise ValueError("url is empty")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES:
        raise ValueError(f"scheme '{scheme}' is not allowed; only http/https")
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme '{scheme}'; only http/https")
    if not parsed.hostname:
        raise ValueError("url has no hostname")
    return url


# ---------------------------------------------------------------------------
# HTML to plain-text extraction (standard library only)
# ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    """Lightweight HTML-to-text extractor that strips scripts and styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in _STRIP_TAGS:
            self._skip_depth += 1
        if lower == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in _STRIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if lower == "title":
            self._in_title = False
        if lower in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and not self._title:
            self._title = data.strip()
        if self._skip_depth == 0:
            self._parts.append(data)

    @property
    def title(self) -> str:
        return self._title

    @property
    def text(self) -> str:
        raw = "".join(self._parts)
        lines = (line.strip() for line in raw.splitlines())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line))


def _extract_text(html_content: str) -> tuple[str, str]:
    """Return ``(title, body_text)`` extracted from raw HTML."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        pass
    return extractor.title, extractor.text


# ---------------------------------------------------------------------------
# Core fetch logic
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FetchedPage:
    url: str
    title: str
    content: str
    response_bytes: int


def _fetch_document(url: str) -> _FetchedPage | str:
    """Retain complete extracted text inside the HTTP byte budget."""
    try:
        url = validate_url(url)
    except ValueError as exc:
        return f"Error: Invalid URL: {exc}"
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=ctx) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/" not in content_type and "html" not in content_type and "json" not in content_type:
                return f"Error: Unsupported content type: {content_type or 'unknown'}. Only text, HTML, and JSON responses are supported."
            raw_bytes = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                return f"Error: Response exceeds the {_MAX_RESPONSE_BYTES} byte budget; complete content is unavailable."
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                body = raw_bytes.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body = raw_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        reason = f" {exc.reason}" if exc.reason else ""
        return f"Error: HTTP {exc.code}{reason}."
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return f"Error: Request timed out after {_TIMEOUT_SECONDS} seconds."
        return f"Error: Network request failed: {exc.reason}"
    except TimeoutError:
        return f"Error: Request timed out after {_TIMEOUT_SECONDS} seconds."
    except Exception as exc:
        return f"Error: Failed to fetch page: {exc}"
    title, text = _extract_text(body) if "html" in content_type.lower() else ("", body)
    return _FetchedPage(url, title, text, len(raw_bytes))


def _format_page(page: _FetchedPage, max_chars: int | None = None) -> str:
    text = page.content if max_chars is None else page.content[:max_chars]
    parts = [f"Title: {page.title or '(not available)'}", f"URL: {page.url}", f"Content:\n{text}"]
    if max_chars is not None and len(page.content) > max_chars:
        parts.append(f"Truncated: showing the first {max_chars} of {len(page.content)} characters. Full content is retained separately.")
    return "\n".join(parts)


def _fetch_page(url: str, max_chars: int) -> str:
    """Return the bounded human-readable preview of one fetched document."""
    document = _fetch_document(url)
    return document if isinstance(document, str) else _format_page(document, max_chars)


# ---------------------------------------------------------------------------
# Tool handler and declaration
# ---------------------------------------------------------------------------

def _handler_web_fetch_page(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult(
            ok=False,
            error="Missing required parameter: url.",
            error_code="url_required",
            stage="validation",
        )
    try:
        max_chars = int(args.get("max_chars", _DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        return ToolResult(
            ok=False,
            error="Invalid max_chars: expected an integer.",
            error_code="max_chars_invalid",
            stage="validation",
        )
    if not 1 <= max_chars <= 50000:
        return ToolResult(ok=False, error="max_chars must be between 1 and 50000; this only limits the preview.",
                          error_code="max_chars_invalid", stage="validation")

    document = _fetch_document(url)
    if isinstance(document, str):
        return ToolResult(
            ok=False,
            error=document.removeprefix("Error:").strip(),
            error_code="web_fetch_failed",
            stage="execution",
            data={"url": url},
        )
    return ToolResult(
        ok=True,
        summary=_format_page(document, max_chars),
        data={"url": url, "content": _format_page(document), "complete": True,
              "response_bytes": document.response_bytes, "content_chars": len(document.content),
              "preview_truncated": len(document.content) > max_chars},
    )


web_fetch_page = ToolDef(
    name="web_fetch_page",
    summary=(
        "Fetch a public or private HTTP(S) webpage/API URL and extract its main text content. "
        "Use when you have a specific URL — either from search results or constructed directly "
        "(e.g. Wikipedia revision history ?action=history, Wikipedia API /w/api.php, "
        "or any URL suggested in a previous tool's next_steps). "
        "Does not render JavaScript; works best on article, documentation, and API response pages."
    ),
    input_schema=object_schema(
        {
            "url": {
                "type": "string",
                "description": "The full URL to fetch (http or https only).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum preview characters (1-50000); full fetched text is retained separately.",
                "default": _DEFAULT_MAX_CHARS,
                "minimum": 1,
                "maximum": 50000,
            },
        },
        required=("url",),
    ),
    output_schema=object_schema(
        {"url": {"type": "string"}, "content": {"type": "string"},
         "complete": {"type": "boolean"}, "response_bytes": {"type": "integer"},
         "content_chars": {"type": "integer"}, "preview_truncated": {"type": "boolean"}},
        required=("url", "content"),
    ),
    handler=_handler_web_fetch_page,
    category=_CATEGORY,
    owner=_OWNER,
    module=__name__,
    access="member",
    weight="light",
    metadata={"result_content_field": "content"},
)

TOOLS: List[ToolDef] = [web_fetch_page]

TOOL_PROVIDER = static_tool_provider(
    "web-fetch",
    packs={"web.fetch": tuple(TOOLS)},
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER", "web_fetch_page"]
