"""Application regression template installed outside the worker's writable scope."""
import asyncio
from pathlib import Path
import sys

from integrations.desktop_app.bridge import application_actions

ROOT = Path(__file__).resolve().parents[2]
BOT = "__APPLICATION_ID__"


def test_desktop_application_exposes_named_operations(tmp_path):
    operations = application_actions(ROOT, BOT, tmp_path / "application")
    assert isinstance(operations, dict) and operations
    assert all(isinstance(name, str) and name and callable(operation)
               for name, operation in operations.items())
    assert (ROOT / "integrations" / BOT / "index.html").is_file()


def test_agent_discovers_the_same_application_operations(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    data = tmp_path / "application"
    expected = set(application_actions(ROOT, BOT, data))

    async def discover():
        server = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "integrations/desktop_app/bridge.py"), "mcp", str(ROOT), BOT, str(data)],
            env={"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
        async with stdio_client(server) as (reader, writer), ClientSession(reader, writer) as client:
            await client.initialize()
            return {tool.name for tool in (await client.list_tools()).tools}

    assert asyncio.run(discover()) == expected
