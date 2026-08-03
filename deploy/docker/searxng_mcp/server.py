from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
HOST = os.environ.get("SEARXNG_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("SEARXNG_MCP_PORT", "8003"))
TIMEOUT_SECONDS = float(os.environ.get("SEARXNG_TIMEOUT_SECONDS", "20"))
MAX_RESULTS = 10
IMAGE_LIMIT = 5


TOOLS = [
    {
        "name": "search",
        "description": "Search the web through the local SearXNG instance without an external API key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "categories": {
                    "type": "string",
                    "description": "SearXNG categories, usually 'general'.",
                    "default": "general",
                },
                "language": {"type": "string", "description": "Language code.", "default": "auto"},
                "time_range": {
                    "type": "string",
                    "enum": ["", "day", "month", "year"],
                    "description": "Optional result time range.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "image_search",
        "description": "Search images through the local SearXNG images category and return direct image candidates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Image search query."},
                "language": {"type": "string", "description": "Language code.", "default": "auto"},
                "safesearch": {
                    "type": "integer",
                    "description": "SearXNG safesearch level: 0 off, 1 moderate, 2 strict.",
                    "default": 1,
                    "minimum": 0,
                    "maximum": 2,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
]


class Handler(BaseHTTPRequestHandler):
    server_version = "chatcopilot-searxng-mcp/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/health", "/mcp"}:
            self._send_json({"ok": True})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/mcp":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            response = handle_rpc(payload)
        except Exception as exc:  # noqa: BLE001
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"},
            }
        self._send_json(response)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def handle_rpc(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = payload.get("id")
    method = payload.get("method")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        result = call_tool(name, arguments)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unsupported method: {method}"},
    }


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "search":
            data = searxng_search(
                query=require_query(arguments),
                categories=str(arguments.get("categories") or "general"),
                language=str(arguments.get("language") or "auto"),
                time_range=str(arguments.get("time_range") or ""),
                safesearch=int(arguments.get("safesearch") or 1),
                limit=bounded_limit(arguments.get("limit")),
            )
            return text_result(format_web_results(data))
        if name == "image_search":
            data = searxng_search(
                query=require_query(arguments),
                categories="images",
                language=str(arguments.get("language") or "auto"),
                time_range="",
                safesearch=int(arguments.get("safesearch") or 1),
                limit=bounded_limit(arguments.get("limit")),
            )
            return text_result(format_image_results(data))
        return error_result(f"unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001
        return error_result(f"{type(exc).__name__}: {exc}")


def require_query(arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    return query


def bounded_limit(value: Any) -> int:
    try:
        limit = int(value or 5)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(MAX_RESULTS, limit))


def searxng_search(
    *,
    query: str,
    categories: str,
    language: str,
    time_range: str,
    safesearch: int,
    limit: int,
) -> dict[str, Any]:
    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "categories": categories,
        "language": language,
        "safesearch": str(max(0, min(2, safesearch))),
    }
    if time_range:
        params["time_range"] = time_range
    url = f"{SEARXNG_URL}/search?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "AgentStrata-SearXNG-MCP/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SearXNG HTTP {exc.code}: {body[:500]}") from exc
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("SearXNG response is not a JSON object")
    data["results"] = (data.get("results") or [])[:limit]
    return data


def format_web_results(data: dict[str, Any]) -> dict[str, Any]:
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            compact_dict(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content": item.get("content"),
                    "source": item.get("engine") or item.get("source"),
                    "score": item.get("score"),
                }
            )
        )
    return {
        "ok": True,
        "source": "searxng",
        "query": data.get("query") or "",
        "count": len(results),
        "results": results,
    }


def format_image_results(data: dict[str, Any]) -> dict[str, Any]:
    results = []
    candidates = []
    seen: set[str] = set()
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        image_url = first_text(item, "img_src", "thumbnail_src", "image", "url")
        source_url = first_text(item, "url", "source_url", "source")
        title = first_text(item, "title", "content")
        source = first_text(item, "engine", "source")
        results.append(
            compact_dict(
                {
                    "title": title,
                    "url": source_url,
                    "image_url": image_url,
                    "thumbnail_url": first_text(item, "thumbnail_src"),
                    "source": source,
                }
            )
        )
        if image_url and image_url not in seen:
            seen.add(image_url)
            candidates.append(
                compact_dict(
                    {
                        "image_url": image_url,
                        "source_url": source_url,
                        "title": title,
                        "source": source or "searxng",
                    }
                )
            )
        if len(candidates) >= IMAGE_LIMIT:
            break
    return {
        "ok": True,
        "source": "searxng",
        "query": data.get("query") or "",
        "count": len(results),
        "results": results,
        "image_candidates": candidates,
    }


def first_text(obj: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def compact_dict(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in obj.items() if value not in ("", None, [], {})}


def text_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "isError": False,
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
    }


def error_result(message: str) -> dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AgentStrata SearXNG MCP listening on {HOST}:{PORT}, SearXNG={SEARXNG_URL}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
