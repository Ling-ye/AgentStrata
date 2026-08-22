"""stdio MCP server：把 AgentStrata agent 工具暴露给上游 MCP 客户端。

启动：
    python -m chatcopilot mcp-server

行为：
- ``list_tools()`` ← ``agent.tools.build_mcp_tools_schema()``
- ``call_tool(name, args)`` ← ``agent.tools.ToolExecutor.execute()``
- 失败时把 error 串成 text 回写，agent 自我修复用
- stdout 仅跑 MCP JSON-RPC；业务模块 print 的内容由 ``_capture_streams`` 转到 stderr
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from chatcopilot.agent.tools import ToolExecutor, ToolResult, build_mcp_tools_schema
from chatcopilot.core.workspace_runtime import (
    cleanup_workspace,
    describe_workspace,
    resolve_workspace,
)
from chatcopilot.project import ENV_PREFIX, PROJECT_SLUG

_LOGGER = logging.getLogger("chatcopilot.middleware.mcp")


def _setup_logging() -> None:
    """统一通过 ``core.logging.configure_logging`` 配置，与 ACP runtime 共享 runtime/<date>.log。"""

    from chatcopilot.core.logging import configure_logging

    configure_logging("INFO", f"{ENV_PREFIX}_MCP_LOG_LEVEL")


def _serialize_tool_result(name: str, result: ToolResult) -> str:
    payload: Dict[str, Any] = {
        "tool": name,
        "ok": result.ok,
    }
    if result.ok:
        payload["summary"] = result.summary
        if result.outputs:
            payload["outputs"] = result.outputs
        if result.console:
            payload["console_tail"] = result.console[-2000:]
        if result.doc_links:
            payload["doc_links"] = result.doc_links
    else:
        payload["error"] = result.error or "unknown error"
        if result.console:
            payload["console_tail"] = result.console[-2000:]
        if result.doc_links:
            payload["doc_links"] = result.doc_links
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _run_stdio_server() -> int:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError as exc:
        sys.stderr.write(
            "[mcp_server] 缺少依赖：python -m pip install 'agentstrata[agent]'\n"
            f"详细：{exc}\n"
        )
        return 2

    tools_schema, _spec_index = build_mcp_tools_schema()
    executor = ToolExecutor()

    server: Server = Server(PROJECT_SLUG)

    @server.list_tools()
    async def _list_tools() -> List[Tool]:
        out: List[Tool] = []
        for entry in tools_schema:
            out.append(
                Tool(
                    name=entry["name"],
                    description=entry["description"],
                    inputSchema=entry["inputSchema"],
                )
            )
        return out

    @server.call_tool()
    async def _call_tool(name: str, arguments: Dict[str, Any] | None) -> List[TextContent]:
        args = arguments or {}
        _LOGGER.info("call_tool name=%s args_keys=%s", name, list(args.keys()))
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, executor.execute, name, args)
        text = _serialize_tool_result(name, result)
        _LOGGER.info("call_tool done name=%s ok=%s", name, result.ok)
        return [TextContent(type="text", text=text)]

    ws = resolve_workspace(create=True)
    _LOGGER.info("MCP server starting | %s | tools=%d", describe_workspace(ws), len(tools_schema))

    try:
        cleanup_summary = cleanup_workspace(ws)
        total_deleted = sum(s["deleted_files"] for s in cleanup_summary.values())
        if total_deleted:
            _LOGGER.info("startup cleanup: %s", cleanup_summary)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("startup cleanup failed (non-fatal)")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
    return 0


def serve() -> int:
    """主入口：被 ``python -m chatcopilot mcp-server`` 调用。"""
    _setup_logging()
    try:
        return asyncio.run(_run_stdio_server())
    except KeyboardInterrupt:
        _LOGGER.info("MCP server interrupted by user")
        return 0
    except Exception:  # noqa: BLE001
        _LOGGER.exception("MCP server crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(serve())
