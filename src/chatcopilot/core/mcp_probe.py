"""Deterministic MCP initialization and tool-schema probe without tool invocation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from chatcopilot.contracts.runtime import McpServerConfig


@dataclass(frozen=True)
class McpProbeTool:
    name: str
    description_sha256: str
    input_schema_sha256: str


@dataclass(frozen=True)
class McpProbeResult:
    ok: bool
    server_id: str
    transport: str
    tools: tuple[McpProbeTool, ...] = ()
    server_name: str = ""
    server_version: str = ""
    error_code: str = ""
    error: str = ""

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "server_id": self.server_id,
            "transport": self.transport,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "tools": [
                {
                    "name": tool.name,
                    "description_sha256": tool.description_sha256,
                    "input_schema_sha256": tool.input_schema_sha256,
                }
                for tool in self.tools
            ],
            "error_code": self.error_code,
            "error": self.error,
        }


def probe_mcp_server(
    config: McpServerConfig,
    *,
    allow_private_network: bool = False,
) -> McpProbeResult:
    """Initialize one server, list and validate schemas, then disconnect."""

    try:
        _validate_probe_target(config, allow_private_network=allow_private_network)
        server_name, server_version, tools = asyncio.run(
            asyncio.wait_for(
                _probe_async(config),
                timeout=max(1.0, float(config.timeout_seconds) + 5.0),
            )
        )
        return McpProbeResult(
            ok=True,
            server_id=config.id,
            transport=config.transport,
            tools=tools,
            server_name=server_name,
            server_version=server_version,
        )
    except TimeoutError as exc:
        return McpProbeResult(
            ok=False,
            server_id=config.id,
            transport=config.transport,
            error_code="probe_timeout",
            error=str(exc) or "MCP probe timed out",
        )
    except Exception as exc:  # noqa: BLE001
        return McpProbeResult(
            ok=False,
            server_id=config.id,
            transport=config.transport,
            error_code="probe_failed",
            error=f"{type(exc).__name__}: {exc}",
        )


async def _probe_async(config: McpServerConfig) -> tuple[str, str, tuple[McpProbeTool, ...]]:
    from mcp import ClientSession

    async with _probe_transport(config) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=config.timeout_seconds),
        ) as session:
            initialized = await session.initialize()
            server_info = getattr(initialized, "serverInfo", None) or getattr(
                initialized,
                "server_info",
                None,
            )
            if isinstance(server_info, dict):
                server_name = str(server_info.get("name") or "").strip()
                server_version = str(server_info.get("version") or "").strip()
            else:
                server_name = str(getattr(server_info, "name", "") or "").strip()
                server_version = str(getattr(server_info, "version", "") or "").strip()
            if not server_name or not server_version:
                raise ValueError("MCP initialize response lacks server name/version")
            if any(
                len(value) > 256 or any(ord(char) < 32 for char in value)
                for value in (server_name, server_version)
            ):
                raise ValueError("MCP initialize server identity is invalid")
            listed = await session.list_tools()
            remote_tools = list(getattr(listed, "tools", []) or [])

    seen: set[str] = set()
    out: list[McpProbeTool] = []
    if len(remote_tools) > 256:
        raise ValueError("MCP tool count exceeds 256")
    for remote in remote_tools:
        name = str(getattr(remote, "name", "") or "").strip()
        if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
            raise ValueError(f"invalid MCP tool name: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate MCP tool name: {name}")
        seen.add(name)
        description = str(getattr(remote, "description", "") or "")
        if len(description.encode("utf-8")) > 64_000:
            raise ValueError(f"tool description exceeds 64KB: {name}")
        schema = getattr(remote, "inputSchema", None) or {}
        if not isinstance(schema, dict):
            raise ValueError(f"tool inputSchema must be an object: {name}")
        if schema.get("type") not in {None, "object"}:
            raise ValueError(f"tool inputSchema.type must be object: {name}")
        encoded_schema = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_schema) > 256_000:
            raise ValueError(f"tool inputSchema exceeds 256KB: {name}")
        out.append(
            McpProbeTool(
                name=name,
                description_sha256=hashlib.sha256(description.encode("utf-8")).hexdigest(),
                input_schema_sha256=hashlib.sha256(encoded_schema).hexdigest(),
            )
        )
    return server_name, server_version, tuple(sorted(out, key=lambda tool: tool.name))


@asynccontextmanager
async def _probe_transport(config: McpServerConfig) -> AsyncIterator[tuple[Any, Any]]:
    if config.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="chatcopilot-mcp-probe-") as home:
            params = StdioServerParameters(
                command=verified_stdio_command(config),
                args=list(config.args),
                env=_minimal_probe_env(config.env, home=home),
                cwd=Path(home),
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
            sse_read_timeout=max(60.0, float(config.timeout_seconds) * 2.0),
        ) as streams:
            yield streams
        return

    if config.transport == "streamable_http":
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            config.url or "",
            headers=config.headers or None,
            timeout=config.timeout_seconds,
            sse_read_timeout=max(60.0, float(config.timeout_seconds) * 2.0),
        ) as streams:
            read_stream, write_stream, _session_id = streams
            yield read_stream, write_stream
        return

    raise ValueError(f"unsupported MCP transport: {config.transport}")


def _minimal_probe_env(declared: dict[str, str], *, home: str) -> dict[str, str]:
    reserved = sorted(set(declared).intersection({"HOME", "PATH", "TMPDIR"}))
    if reserved:
        raise ValueError(
            "declared MCP env may not override probe isolation variables: "
            + ", ".join(reserved)
        )
    env = {
        "HOME": home,
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
    }
    for name in ("LANG", "LC_ALL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    env.update({str(key): str(value) for key, value in declared.items()})
    return env


def _validate_probe_target(
    config: McpServerConfig,
    *,
    allow_private_network: bool,
) -> None:
    if config.transport == "stdio":
        if not (config.command or "").strip():
            raise ValueError("stdio MCP probe requires command")
        return
    parsed = urlparse(config.url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("remote MCP probe requires an HTTP(S) URL")
    if parsed.scheme != "https" and not allow_private_network:
        raise ValueError("public remote MCP probe requires HTTPS")
    if allow_private_network:
        return
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("remote MCP hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(f"remote MCP URL resolves to a non-public address: {ip}")


def verified_stdio_command(config: McpServerConfig) -> str:
    """Resolve and verify a digest-pinned stdio launcher before each start."""

    command_name = (config.command or "").strip()
    if not command_name:
        raise ValueError("stdio MCP requires command")
    approved_digest = config.artifact_digest.strip().lower()
    if not approved_digest:
        return command_name
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", approved_digest):
        raise ValueError("stdio MCP artifact_digest must be sha256:<64 lowercase hex>")
    resolved = shutil.which(command_name)
    direct_path = Path(resolved) if resolved else None
    if (
        direct_path is None
        or direct_path.is_symlink()
        or not direct_path.is_file()
        or not os.access(direct_path, os.X_OK)
    ):
        raise ValueError("digest-pinned stdio MCP command must be an executable regular PATH file")
    command = direct_path.resolve()
    if command.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("digest-pinned stdio MCP launcher exceeds 128MB")
    hasher = hashlib.sha256()
    with command.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = "sha256:" + hasher.hexdigest()
    if actual != approved_digest:
        raise ValueError("stdio MCP launcher digest differs from artifact_digest")
    return str(command)


__all__ = [
    "McpProbeResult",
    "McpProbeTool",
    "probe_mcp_server",
    "verified_stdio_command",
]
