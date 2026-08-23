from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
import uuid
from unittest import mock
from pathlib import Path

from chatcopilot.agent.mcp import client as mcp_client
from chatcopilot.agent.mcp.client import McpToolProvider, _stream_read_timeout
from chatcopilot.agent.mcp.runner import _resolve_stdio_args, _resolve_stdio_command
from chatcopilot.botspec.mcp import McpServerConfig
from chatcopilot.contracts.tools import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


class McpClientProviderTests(unittest.TestCase):
    def test_load_provider_splits_main_and_subagent_exposure(self) -> None:
        def _handler(_arguments, _context):
            return ToolResult(ok=True, data={})

        def _tool(name: str, exposure: str) -> ToolDef:
            return ToolDef(
                name=name,
                summary=name,
                input_schema=object_schema(),
                output_schema=object_schema(),
                handler=_handler,
                category="mcp",
                owner="mcp",
                module=__name__,
                metadata={"mcp_exposure": exposure},
            )

        source = McpToolProvider(())
        with mock.patch.object(
            source,
            "load_tools",
            return_value=(
                _tool("main_search", "main"),
                _tool("private_search", "subagent"),
            ),
        ):
            provider = source.load_provider()

        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertEqual(tuple(provider.packs), ("mcp.dynamic", "mcp.subagent"))
        self.assertEqual(
            [tool.name for tool in provider.packs["mcp.dynamic"]],
            ["main_search"],
        )
        self.assertEqual(
            [tool.name for tool in provider.packs["mcp.subagent"]],
            ["private_search"],
        )

    def test_generic_python_stdio_command_uses_runtime_interpreter(self) -> None:
        self.assertEqual(_resolve_stdio_command("python"), sys.executable)
        self.assertEqual(_resolve_stdio_command("python3"), sys.executable)
        self.assertEqual(_resolve_stdio_command("/opt/custom/python"), "/opt/custom/python")

    def test_stdio_args_expand_runtime_env_and_reject_missing_refs(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_DEV_ROOT": "/srv/chatcopilot"},
            clear=False,
        ):
            self.assertEqual(
                _resolve_stdio_args(
                    ("--repository", "${CHATCOPILOT_DEV_ROOT}", "--mode=readonly")
                ),
                ["--repository", "/srv/chatcopilot", "--mode=readonly"],
            )

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            ValueError,
            "MISSING_MCP_ROOT",
        ):
            _resolve_stdio_args(("${MISSING_MCP_ROOT}",))

    def test_digest_pinned_stdio_is_rechecked_and_resolved_before_start(self) -> None:
        class FakeRemoteTool:
            name = "read"
            description = "read"
            inputSchema = {"type": "object", "properties": {}}

        seen_commands: list[str] = []

        class FakeRunner:
            def __init__(self, config: McpServerConfig) -> None:
                seen_commands.append(config.command or "")

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def stop(self) -> None:
                pass

            def is_running(self) -> bool:
                return True

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "sample-mcp"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            artifact_digest = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
            config = McpServerConfig(
                id="sample",
                command="sample-mcp",
                artifact_digest=artifact_digest,
                allowed_tools=("read",),
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}"},
            ), mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
                tools = McpToolProvider((config,)).load_tools()
                executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                rejected = McpToolProvider((config,)).load_tools()

        self.assertEqual([tool.name for tool in tools], ["read"])
        self.assertEqual(seen_commands, [str(executable.resolve())])
        self.assertEqual(rejected, ())

    def test_server_tool_policy_filters_before_schema_exposure(self) -> None:
        class FakeRemoteTool:
            description = "tool"
            inputSchema = {"type": "object", "properties": {}}

            def __init__(self, name: str) -> None:
                self.name = name

        class FakeRunner:
            def __init__(self, config: McpServerConfig) -> None:
                self.config = config

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool("list_items"), FakeRemoteTool("remove_skill")]

            def stop(self) -> None:
                pass

            def is_running(self) -> bool:
                return True

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="sample-write",
                    tool_prefix="sample_",
                    allowed_tools=("list_items", "remove_skill"),
                    denied_tools=("remove_skill",),
                ),
            )
        )
        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            tools = provider.load_tools()

        self.assertEqual([tool.name for tool in tools], ["sample_list_items"])
        self.assertEqual(tools[0].metadata["mcp_allowed_tools"], ["list_items", "remove_skill"])
        self.assertEqual(tools[0].metadata["mcp_denied_tools"], ["remove_skill"])

    def test_stateful_http_startup_retries_once_after_empty_tool_list(self) -> None:
        class FakeRemoteTool:
            name = "read"
            description = "read"
            inputSchema = {"type": "object", "properties": {}}

        attempts = 0
        stopped: list[int] = []

        class FakeRunner:
            def __init__(self, config: McpServerConfig) -> None:
                nonlocal attempts
                attempts += 1
                self.attempt = attempts

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [] if self.attempt == 1 else [FakeRemoteTool()]

            def stop(self) -> None:
                stopped.append(self.attempt)

            def is_running(self) -> bool:
                return True

        config = McpServerConfig(
            id="remote",
            transport="streamable_http",
            url="https://example.com/mcp",
        )
        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            provider = McpToolProvider((config,))
            tools = provider.load_tools()
            provider.close()

        self.assertEqual([tool.name for tool in tools], ["read"])
        self.assertEqual(attempts, 2)
        self.assertEqual(stopped, [1, 2])

    def test_playwright_arguments_reject_unsafe_operations(self) -> None:
        config = McpServerConfig(id="playwright", risk="interactive")

        with self.assertRaisesRegex(ValueError, "only allows list"):
            mcp_client._normalize_mcp_tool_arguments(
                config,
                "browser_tabs",
                {"action": "new", "url": "https://example.com"},
            )
        with self.assertRaisesRegex(ValueError, "navigation keys"):
            mcp_client._normalize_mcp_tool_arguments(
                config,
                "browser_press_key",
                {"key": "Enter"},
            )
        with self.assertRaisesRegex(ValueError, "filename"):
            mcp_client._normalize_mcp_tool_arguments(
                config,
                "browser_snapshot",
                {"filename": "page.md"},
            )

    def test_playwright_wait_is_clamped(self) -> None:
        config = McpServerConfig(id="playwright", risk="interactive")

        normalized = mcp_client._normalize_mcp_tool_arguments(
            config,
            "browser_wait_for",
            {"time": 120},
        )

        self.assertEqual(normalized["time"], 10)

    def test_stateless_response_empty_body_has_clear_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "empty stateless MCP response"):
            mcp_client._parse_stateless_response("")

    def test_stateless_response_non_json_body_has_clear_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-JSON stateless MCP response"):
            mcp_client._parse_stateless_response("Service Unavailable")

    def test_stateless_streamable_http_lists_and_calls_tools(self) -> None:
        responses = [
            (
                'event: message\n'
                'data: {"result":{"tools":[{"name":"search","description":"Search web",'
                '"inputSchema":{"type":"object","properties":{"query":{"type":"string"}},'
                '"required":["query"]}}]},"jsonrpc":"2.0","id":1}\n\n'
            ),
            (
                'event: message\n'
                'data: {"result":{"content":[{"type":"text","text":"ok"}],'
                '"isError":false},"jsonrpc":"2.0","id":2}\n\n'
            ),
        ]

        class FakeResponse:
            def __init__(self, text: str) -> None:
                self._body = text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return self._body

        def fake_urlopen(request, timeout):  # noqa: ANN001
            return FakeResponse(responses.pop(0))

        config = McpServerConfig(
            id="tavily",
            transport="streamable_http",
            url="http://localhost:18061/mcp",
            tool_prefix="web_",
            stateless_http=True,
            timeout_seconds=5,
        )
        provider = McpToolProvider([config])
        with mock.patch("chatcopilot.agent.mcp.client.urllib.request.urlopen", side_effect=fake_urlopen):
            tools = provider.load_tools()
            by_name = {tool.name: tool for tool in tools}
            self.assertIn("web_search", by_name)
            self.assertEqual(by_name["web_search"].required, ["query"])

            result = by_name["web_search"].handler(
                {"query": "wallpaper"},
                ToolContext(),
            )

        self.assertTrue(result.ok)
        self.assertIn('"text": "ok"', result.summary)
        self.assertEqual(result.outputs, [])
        self.assertIsNone(result.file_type_hint)

    def test_stateless_mcp_error_is_structured_feedback(self) -> None:
        responses = [
            (
                'event: message\n'
                'data: {"result":{"tools":[{"name":"tavily_search","description":"Search web",'
                '"inputSchema":{"type":"object","properties":{"query":{"type":"string"}},'
                '"required":["query"]}}]},"jsonrpc":"2.0","id":1}\n\n'
            ),
            (
                'event: message\n'
                'data: {"result":{"content":[{"type":"text","text":"Tavily API error: '
                '{\\"error\\":\\"This request exceeds your plan\\u0027s set usage limit. '
                'Please upgrade your plan\\"}"}],"isError":true},"jsonrpc":"2.0","id":2}\n\n'
            ),
        ]

        class FakeResponse:
            def __init__(self, text: str) -> None:
                self._body = text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return self._body

        def fake_urlopen(request, timeout):  # noqa: ANN001
            return FakeResponse(responses.pop(0))

        config = McpServerConfig(
            id="tavily",
            transport="streamable_http",
            url="http://localhost:18061/mcp",
            tool_prefix="web_",
            stateless_http=True,
            timeout_seconds=5,
        )
        provider = McpToolProvider([config])
        with mock.patch("chatcopilot.agent.mcp.client.urllib.request.urlopen", side_effect=fake_urlopen):
            tools = provider.load_tools()
            by_name = {tool.name: tool for tool in tools}

            result = by_name["web_tavily_search"].handler(
                {"query": "wallpaper"},
                ToolContext(),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "mcp_quota_exceeded")
        self.assertEqual(result.data["server_id"], "tavily")
        self.assertEqual(result.data["tool_name"], "tavily_search")
        self.assertFalse(result.data["retryable"])
        self.assertEqual(result.outputs, [])
        self.assertIsNone(result.file_type_hint)

    def test_stdio_server_tool_is_wrapped_and_callable(self) -> None:
        tmp_dir = Path(__file__).resolve().parents[2] / "scratch_unit_tests" / f"mcp-{uuid.uuid4().hex}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            server_path = tmp_dir / "fake_mcp_server.py"
            server_path.write_text(
                textwrap.dedent(
                    """\
                    from __future__ import annotations

                    import asyncio

                    from mcp.server import Server
                    from mcp.server.stdio import stdio_server
                    from mcp.types import TextContent, Tool

                    server = Server("fake-search")

                    @server.list_tools()
                    async def list_tools():
                        return [
                            Tool(
                                name="search",
                                description="Search public web pages.",
                                inputSchema={
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string", "description": "Search query."}
                                    },
                                    "required": ["query"],
                                },
                            )
                        ]

                    @server.call_tool()
                    async def call_tool(name, arguments):
                        query = (arguments or {}).get("query", "")
                        return [TextContent(type="text", text=f"result for {query}")]

                    async def main():
                        async with stdio_server() as (read_stream, write_stream):
                            await server.run(
                                read_stream,
                                write_stream,
                                server.create_initialization_options(),
                            )

                    if __name__ == "__main__":
                        asyncio.run(main())
                    """
                ),
                encoding="utf-8",
            )
            provider = McpToolProvider(
                (
                    McpServerConfig(
                        id="web",
                        transport="stdio",
                        command=sys.executable,
                        args=(str(server_path),),
                        tool_prefix="web_",
                        exposure="subagent",
                        allowed_subagents=("web_research",),
                        risk="search",
                        timeout_seconds=10,
                    ),
                )
            )
            try:
                tools = provider.load_tools()
                by_name = {tool.name: tool for tool in tools}
                if "web_search" not in by_name and sys.platform == "win32":
                    self.skipTest(
                        "Windows subprocess pipe creation is blocked in this test environment"
                    )

                self.assertIn("web_search", by_name)
                result = by_name["web_search"].handler(
                    {"query": "unity memory"},
                    ToolContext(),
                )

                self.assertTrue(result.ok)
                self.assertIn("result for unity memory", result.summary)
                self.assertEqual(result.outputs, [])
                self.assertIsNone(result.file_type_hint)
                self.assertEqual(by_name["web_search"].metadata["mcp_exposure"], "subagent")
                self.assertEqual(by_name["web_search"].metadata["mcp_allowed_subagents"], ["web_research"])
                self.assertEqual(by_name["web_search"].metadata["mcp_risk"], "search")
            finally:
                provider.close()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_reconnects_once_when_runner_loop_closed(self) -> None:
        class FakeRemoteTool:
            name = "search"
            description = "Search public web pages."
            inputSchema = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }

        class FakeRunner:
            instances: list["FakeRunner"] = []

            def __init__(self, config: McpServerConfig) -> None:
                self.config = config
                self.stopped = False
                self.calls = 0
                FakeRunner.instances.append(self)

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                self.calls += 1
                if self is FakeRunner.instances[0]:
                    raise RuntimeError("Event loop is closed")
                return f"recovered {name} for {arguments['query']}"

            def stop(self) -> None:
                self.stopped = True

            def is_running(self) -> bool:
                return not self.stopped

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="xiaohongshu",
                    transport="streamable_http",
                    url="http://localhost:18060/mcp",
                    tool_prefix="xhs_",
                    exposure="subagent",
                    allowed_subagents=("web_research",),
                    risk="search",
                    timeout_seconds=1,
                ),
            )
        )
        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            try:
                tools = provider.load_tools()
                by_name = {tool.name: tool for tool in tools}

                result = by_name["xhs_search"].handler(
                    {"query": "成都29所"},
                    ToolContext(),
                )

                self.assertTrue(result.ok)
                self.assertIn("recovered search for 成都29所", result.summary)
                self.assertEqual(result.outputs, [])
                self.assertIsNone(result.file_type_hint)
                self.assertEqual(len(FakeRunner.instances), 2)
                self.assertTrue(FakeRunner.instances[0].stopped)
            finally:
                provider.close()

    def test_stream_read_timeout_is_longer_than_tool_timeout(self) -> None:
        config = McpServerConfig(
            id="xiaohongshu",
            transport="streamable_http",
            url="http://localhost:18060/mcp",
            timeout_seconds=30,
        )

        self.assertEqual(_stream_read_timeout(config), 300.0)

    def test_timeout_is_not_retried_and_returns_structured_error(self) -> None:
        class FakeRemoteTool:
            name = "search_feeds"
            description = "Search Xiaohongshu feeds."
            inputSchema = {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            }

        class FakeRunner:
            instances: list["FakeRunner"] = []

            def __init__(self, config: McpServerConfig) -> None:
                self.config = config
                self.stopped = False
                self.calls = 0
                FakeRunner.instances.append(self)

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                self.calls += 1
                raise TimeoutError("Timed out while waiting for response")

            def stop(self) -> None:
                self.stopped = True

            def is_running(self) -> bool:
                return not self.stopped

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="xiaohongshu",
                    transport="streamable_http",
                    url="http://localhost:18060/mcp",
                    tool_prefix="xhs_",
                    exposure="subagent",
                    allowed_subagents=("web_research",),
                    risk="search",
                    timeout_seconds=90,
                ),
            )
        )
        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            try:
                tools = provider.load_tools()
                by_name = {tool.name: tool for tool in tools}

                result = by_name["xhs_search_feeds"].handler(
                    {"keyword": "青山制面 上海"},
                    ToolContext(),
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "mcp_timeout")
                self.assertEqual(result.data["server_id"], "xiaohongshu")
                self.assertEqual(result.data["tool_name"], "search_feeds")
                self.assertFalse(result.data["retryable"])
                self.assertEqual(result.outputs, [])
                self.assertIsNone(result.file_type_hint)
                self.assertEqual(len(FakeRunner.instances), 1)
                self.assertEqual(FakeRunner.instances[0].calls, 1)
                self.assertTrue(FakeRunner.instances[0].stopped)
            finally:
                provider.close()

    def test_xiaohongshu_search_args_drop_default_filters(self) -> None:
        class FakeRemoteTool:
            name = "search_feeds"
            description = "Search Xiaohongshu feeds."
            inputSchema = {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "filters": {"type": "object"},
                },
                "required": ["keyword"],
            }

        class FakeRunner:
            instances: list["FakeRunner"] = []

            def __init__(self, config: McpServerConfig) -> None:
                self.config = config
                self.stopped = False
                self.seen_arguments: dict[str, object] | None = None
                FakeRunner.instances.append(self)

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                self.seen_arguments = dict(arguments)
                return '{"is_error": false, "content": [{"type": "text", "text": "ok"}]}'

            def stop(self) -> None:
                self.stopped = True

            def is_running(self) -> bool:
                return not self.stopped

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="xiaohongshu",
                    transport="streamable_http",
                    url="http://localhost:18060/mcp",
                    tool_prefix="xhs_",
                    exposure="subagent",
                    allowed_subagents=("web_research",),
                    risk="search",
                    timeout_seconds=30,
                ),
            )
        )
        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            try:
                tools = provider.load_tools()
                by_name = {tool.name: tool for tool in tools}

                by_name["xhs_search_feeds"].handler(
                    {
                        "keyword": "青山制面 上海",
                        "filters": {
                            "sort_by": "综合",
                            "note_type": "不限",
                            "publish_time": "不限",
                            "location": "同城",
                            "search_scope": "",
                        },
                    },
                    ToolContext(),
                )

                self.assertEqual(
                    FakeRunner.instances[0].seen_arguments,
                    {"keyword": "青山制面 上海", "filters": {"location": "同城"}},
                )
            finally:
                provider.close()

    def test_xiaohongshu_browser_error_is_structured(self) -> None:
        class FakeRemoteTool:
            name = "search_feeds"
            description = "Search Xiaohongshu feeds."
            inputSchema = {"type": "object", "properties": {}, "required": []}

        class FakeRunner:
            def __init__(self, config: McpServerConfig) -> None:
                self.config = config
                self.stopped = False

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                return json.dumps(
                    {
                        "is_error": True,
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "navigation failed: net:"
                                    ":ERR_NAME_NOT_RESOLVED"
                                ),
                            }
                        ],
                    }
                )

            def stop(self) -> None:
                self.stopped = True

            def is_running(self) -> bool:
                return not self.stopped

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="xiaohongshu",
                    transport="streamable_http",
                    url="http://localhost:18060/mcp",
                    tool_prefix="xhs_",
                    exposure="subagent",
                    allowed_subagents=("web_research",),
                    risk="search",
                    timeout_seconds=30,
                ),
            )
        )
        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            try:
                tools = provider.load_tools()
                by_name = {tool.name: tool for tool in tools}

                result = by_name["xhs_search_feeds"].handler({}, ToolContext())

                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "xhs_browser_error")
                self.assertEqual(result.data["server_id"], "xiaohongshu")
                self.assertFalse(result.data["retryable"])
            finally:
                provider.close()


    def test_max_concurrency_serializes_calls(self) -> None:
        """Two concurrent calls to a max_concurrency=1 server are serialized."""

        call_log: list[tuple[str, float]] = []

        class FakeRemoteTool:
            name = "search_feeds"
            description = "Search."
            inputSchema = {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            }

        class FakeRunner:
            def __init__(self, config: McpServerConfig) -> None:
                self.config = config
                self.stopped = False

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                import time as _time

                start = _time.monotonic()
                _time.sleep(0.3)
                call_log.append((str(arguments.get("keyword", "")), start))
                return '{"is_error": false, "content": [{"type": "text", "text": "ok"}]}'

            def stop(self) -> None:
                self.stopped = True

            def is_running(self) -> bool:
                return not self.stopped

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="serialized-svc",
                    transport="streamable_http",
                    url="http://localhost:18060/mcp",
                    tool_prefix="xhs_",
                    exposure="subagent",
                    risk="search",
                    timeout_seconds=10,
                    max_concurrency=1,
                ),
            )
        )
        import threading as _threading

        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            try:
                tools = provider.load_tools()
                handler = {tool.name: tool for tool in tools}["xhs_search_feeds"].handler

                errors: list[Exception] = []

                def _call(kw: str) -> None:
                    try:
                        handler({"keyword": kw}, ToolContext())
                    except Exception as exc:
                        errors.append(exc)

                t1 = _threading.Thread(target=_call, args=("first",))
                t2 = _threading.Thread(target=_call, args=("second",))
                t1.start()
                import time as _time

                _time.sleep(0.05)
                t2.start()
                t1.join(timeout=15)
                t2.join(timeout=15)

                self.assertEqual(len(errors), 0)
                self.assertEqual(len(call_log), 2)
                # Second call must start after first finishes (serialized)
                first_start = call_log[0][1]
                second_start = call_log[1][1]
                self.assertGreater(second_start - first_start, 0.2)
            finally:
                provider.close()

    def test_zero_max_concurrency_allows_parallel_calls(self) -> None:
        """max_concurrency=0 (default) allows fully parallel calls."""

        call_log: list[tuple[str, float]] = []

        class FakeRemoteTool:
            name = "search"
            description = "Search."
            inputSchema = {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            }

        class FakeRunner:
            def __init__(self, config: McpServerConfig) -> None:
                self.config = config
                self.stopped = False

            def start_and_list_tools(self) -> list[FakeRemoteTool]:
                return [FakeRemoteTool()]

            def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                import time as _time

                start = _time.monotonic()
                _time.sleep(0.3)
                call_log.append((str(arguments.get("q", "")), start))
                return '{"is_error": false, "content": [{"type": "text", "text": "ok"}]}'

            def stop(self) -> None:
                self.stopped = True

            def is_running(self) -> bool:
                return not self.stopped

        provider = McpToolProvider(
            (
                McpServerConfig(
                    id="parallel-svc",
                    transport="streamable_http",
                    url="http://localhost:18061/mcp",
                    tool_prefix="web_",
                    exposure="subagent",
                    risk="search",
                    timeout_seconds=10,
                    max_concurrency=0,
                ),
            )
        )
        import threading as _threading

        with mock.patch.object(mcp_client, "_McpServerRunner", FakeRunner):
            try:
                tools = provider.load_tools()
                handler = {tool.name: tool for tool in tools}["web_search"].handler

                t1 = _threading.Thread(
                    target=lambda: handler({"q": "a"}, ToolContext())
                )
                t2 = _threading.Thread(
                    target=lambda: handler({"q": "b"}, ToolContext())
                )
                t1.start()
                t2.start()
                t1.join(timeout=10)
                t2.join(timeout=10)

                self.assertEqual(len(call_log), 2)
                # Both calls should start roughly at the same time (parallel)
                self.assertLess(abs(call_log[0][1] - call_log[1][1]), 0.2)
            finally:
                provider.close()

    def test_compaction_preserves_short_image_candidates(self) -> None:
        payload = {
            "is_error": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "feeds": [
                                {
                                    "title": "noodle shop",
                                    "imageList": [
                                        {"url": "https://cdn.example.com/noodle.jpg?x=1"}
                                    ],
                                    "avatar": "https://cdn.example.com/avatar.jpg",
                                    "click_url": "https://tracking.example.com/long",
                                }
                            ],
                            "goods_list": [
                                {
                                    "goods_name": "coffee",
                                    "goods_image_url": "https://img.example.com/coffee.webp",
                                    "coupon_share_url": "https://tracking.example.com/coupon",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

        compacted = json.loads(mcp_client._compact_mcp_response(payload, 20000))
        data = json.loads(compacted["content"][0]["text"])

        candidates = data["image_candidates"]
        self.assertEqual(
            [item["image_url"] for item in candidates],
            [
                "https://cdn.example.com/noodle.jpg?x=1",
                "https://img.example.com/coffee.webp",
            ],
        )
        self.assertNotIn("imageList", data["feeds"][0])
        self.assertNotIn("avatar", data["feeds"][0])
        self.assertNotIn("click_url", data["feeds"][0])
        self.assertNotIn("goods_image_url", data["goods_list"][0])
        self.assertNotIn("coupon_share_url", data["goods_list"][0])


if __name__ == "__main__":
    unittest.main()
