from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock


_SERVER_PATH = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "searxng_mcp" / "server.py"
_SPEC = importlib.util.spec_from_file_location("searxng_mcp_server", _SERVER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
searxng_mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(searxng_mcp)


def test_tools_list_exposes_search_and_image_search() -> None:
    response = searxng_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search", "image_search"]
    assert tools[1]["inputSchema"]["required"] == ["query"]


def test_web_search_formats_short_results() -> None:
    with mock.patch.object(
        searxng_mcp,
        "searxng_search",
        return_value={
            "query": "chatcopilot",
            "results": [
                {
                    "title": "AgentStrata",
                    "url": "https://example.com/chatcopilot",
                    "content": "A bot platform.",
                    "engine": "duckduckgo",
                }
            ],
        },
    ):
        response = searxng_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "chatcopilot", "limit": 3}},
            }
        )

    result = response["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["source"] == "searxng"
    assert payload["results"][0]["url"] == "https://example.com/chatcopilot"


def test_image_search_returns_image_candidates() -> None:
    with mock.patch.object(
        searxng_mcp,
        "searxng_search",
        return_value={
            "query": "鸢一折纸 壁纸",
            "results": [
                {
                    "title": "Origami wallpaper",
                    "url": "https://wall.example/page",
                    "img_src": "https://img.example/origami.jpg",
                    "thumbnail_src": "https://img.example/thumb.jpg",
                    "engine": "bing images",
                },
                {
                    "title": "duplicate",
                    "url": "https://wall.example/dup",
                    "img_src": "https://img.example/origami.jpg",
                    "engine": "bing images",
                },
            ],
        },
    ):
        response = searxng_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "image_search", "arguments": {"query": "鸢一折纸 壁纸"}},
            }
        )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["image_candidates"] == [
        {
            "image_url": "https://img.example/origami.jpg",
            "source_url": "https://wall.example/page",
            "title": "Origami wallpaper",
            "source": "bing images",
        }
    ]


def test_search_error_is_mcp_error_result() -> None:
    with mock.patch.object(searxng_mcp, "searxng_search", side_effect=RuntimeError("SearXNG HTTP 403")):
        response = searxng_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "image_search", "arguments": {"query": "x"}},
            }
        )

    assert response["result"]["isError"] is True
    assert "SearXNG HTTP 403" in response["result"]["content"][0]["text"]


def test_unknown_rpc_method_returns_jsonrpc_error() -> None:
    response = searxng_mcp.handle_rpc({"jsonrpc": "2.0", "id": 9, "method": "unknown"})

    assert response["error"]["code"] == -32601
