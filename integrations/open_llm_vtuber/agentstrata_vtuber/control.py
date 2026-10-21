"""Mount the avatar's optional MCP surface in the client's existing ASGI process."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import ipaddress
import json

from .avatar import AvatarController
from .config import AdapterConfig


class LocalBoundary:
    """Browser origin and backend-only MCP credential checks at the actual I/O edge."""

    def __init__(self, app, *, config: AdapterConfig):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        origin = headers.get(b"origin", b"").decode("latin-1")
        peer = scope.get("client")
        try:
            local = bool(peer and ipaddress.ip_address(peer[0]).is_loopback)
        except ValueError:
            local = False
        allowed = local and (origin in self.config.allowed_origins if origin else scope["type"] != "websocket")
        if scope["path"].startswith("/agentstrata/mcp"):
            supplied = headers.get(b"authorization", b"")
            expected = ("Bearer " + (self.config.mcp_token or "")).encode()
            allowed = allowed and self.config.mcp_token is not None and hmac.compare_digest(supplied, expected)
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403, "headers": []})
                await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        await self.app(scope, receive, send)


def install_control(app, config: AdapterConfig, controller: AvatarController) -> None:
    app.add_middleware(LocalBoundary, config=config)
    if config.mcp_token is None:
        return
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent
    from chatcopilot.project import format_legal_info

    mcp = FastMCP("AgentStrata avatar", streamable_http_path="/", stateless_http=True,
                 json_response=True, instructions=format_legal_info())

    def action_result(result: dict):
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))],
                              structuredContent=result, isError=result["status"] != "executed")

    @mcp.tool()
    def list_actions() -> dict:
        """List this configured avatar's expressions, motions and connection status."""
        return controller.list_actions()

    @mcp.tool()
    async def set_expression(name: str):
        """Set a registered expression. Only status=executed confirms browser application."""
        return action_result(await controller.set_expression(name))

    @mcp.tool()
    async def play_motion(name: str):
        """Request a registered motion; inspect status, including unknown. Repeating may replay it."""
        return action_result(await controller.play_motion(name))

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            async with mcp.session_manager.run():
                yield

    app.router.lifespan_context = lifespan
    # This is installed before the upstream catch-all static frontend mount.
    app.mount("/agentstrata/mcp", mcp.streamable_http_app())
