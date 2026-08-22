"""External MCP client integration for Agent tools."""
from __future__ import annotations

import logging
import threading
import urllib
from typing import Any, Dict, List

from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.core.mcp_probe import verified_stdio_command
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.agent.mcp.concurrency import _cross_process_lock
from chatcopilot.agent.mcp.arguments import _normalize_mcp_tool_arguments
from chatcopilot.agent.mcp.errors import (
    McpToolBusyError,
    McpToolTimeoutError,
    _is_timeout_error,
)
from chatcopilot.agent.mcp.runner import _McpServerRunner, _stream_read_timeout
from chatcopilot.agent.mcp.serialization import _compact_mcp_response
from chatcopilot.agent.mcp.stateless import _parse_stateless_response
from chatcopilot.agent.mcp.tool_wrapper import _safe_tool_name, _wrap_remote_tool

_LOGGER = logging.getLogger("chatcopilot.agent.mcp.client")


class McpToolProvider:
    """Connect to configured external MCP servers and expose their tools."""

    def __init__(self, servers: tuple[McpServerConfig, ...]) -> None:
        self._servers = servers
        self._runners: dict[str, _McpServerRunner] = {}
        self._lock = threading.RLock()

    def load_tools(self) -> tuple[ToolDef, ...]:
        tools: list[ToolDef] = []
        seen: set[str] = set()
        for config in self._servers:
            try:
                runner, remote_tools = self._start_runner(config)
            except ValueError as exc:
                _LOGGER.error(
                    "skip MCP server that failed runtime policy | server=%s error=%s",
                    config.id,
                    exc,
                )
                continue
            if not remote_tools:
                continue
            for remote_tool in remote_tools:
                remote_name = str(getattr(remote_tool, "name", "") or "").strip()
                if not _remote_tool_allowed(config, remote_name):
                    _LOGGER.info(
                        "skip MCP tool outside server policy | server=%s remote=%s",
                        config.id,
                        remote_name,
                    )
                    continue
                local_name = _safe_tool_name(config.tool_prefix + remote_tool.name)
                if not local_name:
                    _LOGGER.warning("skip MCP tool with invalid name | server=%s remote=%s", config.id, remote_tool.name)
                    continue
                if local_name in seen:
                    _LOGGER.warning("skip duplicate MCP tool | server=%s tool=%s", config.id, local_name)
                    continue
                seen.add(local_name)
                tools.append(_wrap_remote_tool(config, self, remote_tool, local_name))
        return tuple(tools)

    def close(self) -> None:
        with self._lock:
            runners = tuple(self._runners.values())
            self._runners.clear()
        for runner in runners:
            runner.stop()

    def status(self) -> list[dict[str, Any]]:
        """Return a health summary for each configured MCP server."""
        out: list[dict[str, Any]] = []
        with self._lock:
            for config in self._servers:
                runner = self._runners.get(config.id)
                out.append({
                    "id": config.id,
                    "transport": config.transport,
                    "running": runner.is_running() if runner else False,
                    "error": runner._error if runner else None,
                    "tools_count": len(runner._tools) if runner else 0,
                    "max_concurrency": config.max_concurrency,
                })
        return out

    def call_tool(self, config: McpServerConfig, name: str, arguments: Dict[str, Any]) -> str:
        """Call a remote tool, reconnecting once if the transport died after startup.

        Timeout errors are distinguished from connection failures: a timeout
        means the specific browser/API operation is stuck or slow. Do not issue
        the same operation again, because browser-backed MCP tools can keep
        working after the client has already timed out.

        Servers with ``max_concurrency > 0`` are guarded by a cross-process
        advisory file lock so that multiple bot processes sharing the same
        underlying service (e.g. a single-browser MCP container) are serialized.
        """
        if config.max_concurrency > 0:
            with _cross_process_lock(config.id, timeout=config.timeout_seconds):
                return self._call_tool_inner(config, name, arguments)
        return self._call_tool_inner(config, name, arguments)

    def _call_tool_inner(
        self, config: McpServerConfig, name: str, arguments: Dict[str, Any]
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(2):
            runner = self._ensure_runner(config)
            try:
                return runner.call_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                is_timeout = _is_timeout_error(exc)
                _LOGGER.warning(
                    "MCP tool call %s | server=%s tool=%s attempt=%d timeout=%s error=%s",
                    "timed out" if is_timeout else "failed",
                    config.id,
                    name,
                    attempt + 1,
                    is_timeout,
                    exc,
                )
                self._discard_runner(config, runner)
                if is_timeout and not config.retry_on_timeout:
                    break

        timeout_s = config.timeout_seconds
        if _is_timeout_error(last_error):
            raise McpToolTimeoutError(
                f"MCP tool call timed out after {timeout_s}s: "
                f"{config.id}/{name}. The call was not retried to avoid "
                f"stacking duplicate browser automation work.",
                server_id=config.id,
                tool_name=name,
                timeout_seconds=timeout_s,
            ) from last_error
        raise RuntimeError(f"MCP tool call failed after reconnect: {config.id}/{name}") from last_error

    def _ensure_runner(self, config: McpServerConfig) -> "_McpServerRunner":
        with self._lock:
            runner = self._runners.get(config.id)
            if runner is not None and runner.is_running():
                return runner
            if runner is not None:
                runner.stop()
                self._runners.pop(config.id, None)
            runner, remote_tools = self._start_runner_locked(config)
            if not remote_tools:
                runner.stop()
                raise RuntimeError(f"MCP server is not connected: {config.id}")
            self._runners[config.id] = runner
            return runner

    def _start_runner(self, config: McpServerConfig) -> tuple["_McpServerRunner", list[Any]]:
        with self._lock:
            runner, remote_tools = self._start_runner_locked(config)
            if (
                not remote_tools
                and config.transport in {"sse", "streamable_http"}
                and not config.stateless_http
            ):
                runner.stop()
                _LOGGER.warning(
                    "retry MCP stateful HTTP startup once | server=%s transport=%s",
                    config.id,
                    config.transport,
                )
                runner, remote_tools = self._start_runner_locked(config)
            if not remote_tools:
                runner.stop()
                return runner, []
            old_runner = self._runners.get(config.id)
            if old_runner is not None and old_runner is not runner:
                old_runner.stop()
            self._runners[config.id] = runner
            return runner, remote_tools

    def _start_runner_locked(self, config: McpServerConfig) -> tuple["_McpServerRunner", list[Any]]:
        if config.transport == "stdio" and config.artifact_digest:
            from dataclasses import replace

            config = replace(config, command=verified_stdio_command(config))
        runner = _McpServerRunner(config)
        remote_tools = runner.start_and_list_tools()
        return runner, remote_tools

    def _discard_runner(self, config: McpServerConfig, runner: "_McpServerRunner") -> None:
        with self._lock:
            if self._runners.get(config.id) is runner:
                self._runners.pop(config.id, None)
            runner.stop()


def _remote_tool_allowed(config: McpServerConfig, remote_name: str) -> bool:
    """Apply the server's fail-closed tool policy before wrapping schemas."""

    if not remote_name:
        return False
    if remote_name in config.denied_tools:
        return False
    return not config.allowed_tools or remote_name in config.allowed_tools


def list_mcp_tools() -> List[ToolDef]:
    """Backward-compatible empty hook for older callers."""
    return []


def call_mcp_tool(name: str, arguments: Dict[str, Any]) -> Any:
    raise NotImplementedError("MCP tools are now routed through McpToolProvider handlers.")


__all__ = [
    "McpToolBusyError",
    "McpToolProvider",
    "McpToolTimeoutError",
    "_compact_mcp_response",
    "_normalize_mcp_tool_arguments",
    "_parse_stateless_response",
    "_remote_tool_allowed",
    "_stream_read_timeout",
    "call_mcp_tool",
    "list_mcp_tools",
    "urllib",
]
