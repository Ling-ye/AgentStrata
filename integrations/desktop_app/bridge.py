"""Local desktop UI and MCP reach the same application operations."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import re
import sys


def application_actions(root: Path, bot: str, data: Path):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", bot):
        raise ValueError("Invalid application id")
    from chatcopilot.core.private_sqlite import private_directory
    module = root / "integrations" / bot / "actions.py"
    if module.resolve() != module:
        raise ValueError("Invalid application code path")
    spec = importlib.util.spec_from_file_location("desktop_application_actions", module)
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    return implementation.actions(private_directory(data))


async def chat(url: str, token: str, client_id: str, session: str, request: str, message: str):
    from chatcopilot.contracts.gateway_rpc import ChatSendParams, RunsGetParams, SessionsCreateParams, TextRpcSegment
    from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient
    client = GatewayWebSocketClient(GatewayClientConfig(url, token, client_id=client_id, client_mode="interactive"))
    try:
        await client.ensure_connected()
        await client.request("sessions.create", SessionsCreateParams(session), idempotency_key="session-" + session)
        sent = await client.request("chat.send", ChatSendParams(session, (TextRpcSegment(message),)),
                                    idempotency_key="input-" + request)
        while True:
            run = (await client.request("runs.get", RunsGetParams(session, sent.run_id))).run
            if run.state == "completed":
                return "".join(segment.text for segment in run.segments if isinstance(segment, TextRpcSegment))
            if run.state not in {"accepted", "running", "abort_requested"}:
                raise ValueError(run.error_code or "对话未完成：" + run.state)
            await asyncio.sleep(0.35)
    finally:
        await client.close()


def main():
    mode, root_arg, *args = sys.argv[1:]
    root = Path(root_arg).resolve(strict=True)
    if mode == "mcp":
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("Desktop application")
        for name, fn in application_actions(root, args[0], Path(args[1])).items():
            server.add_tool(fn, name=name)
        server.run()
        return
    from chatcopilot.core.application_installation import installation
    from chatcopilot.core.settings import load_local_env_values
    installed = installation(root)
    if not installed:
        raise ValueError("Application is not installed")
    values = load_local_env_values(installed.control / "runtime.env", expand_home=True)
    payload = json.load(sys.stdin)
    if mode == "chat":
        result = asyncio.run(chat(values["CHATCOPILOT_GATEWAY_URL"], values["AGENTSTRATA_DESKTOP_GATEWAY_TOKEN"],
                                  "desktop-owner", "desktop-main", payload["request"], payload["text"]))
    elif mode == "action":
        methods = application_actions(root, installed.bot_id, Path(values["AGENTSTRATA_DESKTOP_DATA"]))
        result = methods[payload["name"]](**payload["args"])
    else:
        raise ValueError("Unknown desktop operation")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
