"""Stateful MCP server runner and transport lifecycle."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.agent.mcp.serialization import _serialize_call_result
from chatcopilot.agent.mcp.stateless import _stateless_call_tool, _stateless_list_tools

_LOGGER = logging.getLogger("chatcopilot.agent.mcp.client")

_RUNNER_STOP_GRACE_SECONDS = 5.0
_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_stdio_command(command: str) -> str:
    """Bind generic Python MCP commands to the active runtime interpreter."""
    normalized = str(command or "").strip()
    if normalized in {"python", "python3"}:
        return sys.executable
    return normalized


def _resolve_stdio_args(args: tuple[str, ...]) -> list[str]:
    """Expand runtime env references after BotSpec runtime env is installed."""
    resolved: list[str] = []
    for raw in args:
        text = str(raw)
        missing = sorted(
            {
                match.group(1)
                for match in _ENV_TOKEN_RE.finditer(text)
                if os.environ.get(match.group(1)) is None
            }
        )
        if missing:
            raise ValueError(
                "unresolved MCP stdio argument environment reference(s): "
                + ", ".join(missing)
            )
        resolved.append(os.path.expandvars(text))
    return resolved


class _McpServerRunner:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._tools: list[Any] = []
        self._error: str = ""
        self._thread: threading.Thread | None = None
        self._inflight_future: asyncio.Future[Any] | None = None

    def start_and_list_tools(self) -> list[Any]:
        if self.config.stateless_http:
            try:
                self._tools = _stateless_list_tools(self.config)
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning("MCP stateless server unavailable | server=%s error=%s", self.config.id, self._error)
                return []
            _LOGGER.info(
                "MCP stateless server connected | server=%s transport=%s tools=%d",
                self.config.id,
                self.config.transport,
                len(self._tools),
            )
            return list(self._tools)
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"mcp-{self.config.id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=max(1.0, self.config.timeout_seconds + 5.0)):
            self._error = "MCP server startup timed out"
            _LOGGER.warning("MCP server startup timed out | server=%s", self.config.id)
            return []
        if self._error:
            _LOGGER.warning("MCP server unavailable | server=%s error=%s", self.config.id, self._error)
            return []
        return list(self._tools)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        if self.config.stateless_http:
            return _stateless_call_tool(self.config, name, arguments)
        if not self.is_running():
            raise RuntimeError(f"MCP server is not connected: {self.config.id}")
        if self._loop is None or self._session is None:
            raise RuntimeError(f"MCP server is not connected: {self.config.id}")
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(name, arguments),
            self._loop,
        )
        self._inflight_future = future
        try:
            return future.result(timeout=max(1.0, self.config.timeout_seconds + 5.0))
        except TimeoutError:
            future.cancel()
            raise
        finally:
            self._inflight_future = None

    def stop(self) -> None:
        # Cancel any in-flight tool call first so the runner thread can exit
        # its async context managers cleanly.
        fut = self._inflight_future
        if fut is not None and not fut.done():
            fut.cancel()
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._closed.wait(timeout=_RUNNER_STOP_GRACE_SECONDS)
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                _LOGGER.warning(
                    "MCP runner thread did not exit in time | server=%s",
                    self.config.id,
                )

    def is_running(self) -> bool:
        if self.config.stateless_http:
            return bool(self._tools) and not self._error
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._session is not None
            and not self._closed.is_set()
            and not self._error
        )

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            _LOGGER.exception("MCP server runner crashed | server=%s", self.config.id)
            self._ready.set()
        finally:
            self._closed.set()
            loop.close()

    async def _run(self) -> None:
        from mcp import ClientSession

        self._stop_event = asyncio.Event()
        async with _open_transport(self.config) as (read_stream, write_stream):
            self._session = ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self.config.timeout_seconds),
            )
            async with self._session:
                await self._session.initialize()
                listed = await self._session.list_tools()
                self._tools = list(getattr(listed, "tools", []) or [])
                _LOGGER.info(
                    "MCP server connected | server=%s transport=%s tools=%d",
                    self.config.id,
                    self.config.transport,
                    len(self._tools),
                )
                self._ready.set()
                await self._stop_event.wait()

    async def _call_tool_async(self, name: str, arguments: Dict[str, Any]) -> str:
        result = await self._session.call_tool(
            name,
            arguments or {},
            read_timeout_seconds=timedelta(seconds=self.config.timeout_seconds),
        )
        return _serialize_call_result(result, max_chars=self.config.max_result_chars)


@asynccontextmanager
async def _open_transport(config: McpServerConfig) -> AsyncIterator[tuple[Any, Any]]:
    if config.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=_resolve_stdio_command(config.command or ""),
            args=_resolve_stdio_args(config.args),
            env={**os.environ, **config.env} if config.env else None,
            cwd=Path(config.cwd) if config.cwd else None,
        )
        async with stdio_client(params) as streams:
            yield streams
        return

    if config.transport == "sse":
        from mcp.client.sse import sse_client

        async with sse_client(
            config.url or "",
            headers=config.headers or None,
            timeout=config.timeout_seconds,
            sse_read_timeout=_stream_read_timeout(config),
        ) as streams:
            yield streams
        return

    if config.transport == "streamable_http":
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            config.url or "",
            headers=config.headers or None,
            timeout=config.timeout_seconds,
            sse_read_timeout=_stream_read_timeout(config),
        ) as streams:
            read_stream, write_stream, _get_session_id = streams
            yield read_stream, write_stream
        return

    raise ValueError(f"unsupported MCP transport: {config.transport}")


def _stream_read_timeout(config: McpServerConfig) -> float:
    # Streamable HTTP keeps a long-polling read open while normal POST tool
    # calls complete on the side. Treat it as transport liveness, not as the
    # per-tool deadline, or slow/quiet MCP servers will close healthy sessions.
    return max(300.0, float(config.timeout_seconds) * 4.0)


__all__ = [
    "_McpServerRunner",
    "_resolve_stdio_args",
    "_resolve_stdio_command",
    "_stream_read_timeout",
]
