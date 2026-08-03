"""MCP server stdio 烟雾测试：发送 initialize + list_tools，校验能返回工具列表。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


MT_HOME = Path(os.environ.get("CHATCOPILOT_HOME", Path.home() / "ChatCopilot"))
PY = MT_HOME / ".venv" / "bin" / "python"


def main() -> int:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    stdin = "\n".join(json.dumps(x, ensure_ascii=False) for x in (init, initialized, list_tools)) + "\n"

    proc = subprocess.Popen(
        [str(PY), "-m", "chatcopilot", "mcp-server"],
        cwd=str(MT_HOME),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": f"{MT_HOME / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
            "CHATCOPILOT_MCP_LOG_LEVEL": "INFO",
            "CHATCOPILOT_WORKSPACE_ROOT": os.environ.get(
                "CHATCOPILOT_WORKSPACE_ROOT",
                str(Path.home() / "chatcopilot-workspaces"),
            ),
        },
        text=True,
    )

    try:
        out, err = proc.communicate(input=stdin, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()

    print("---- STDERR (last 20 lines) ----")
    for line in err.strip().splitlines()[-20:]:
        print(line)

    print("---- STDOUT (raw) ----")
    print(out[:200])
    print("...")

    print("---- TOOL NAMES PARSED ----")
    tool_names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2 and "result" in obj:
            tools = obj["result"].get("tools", [])
            for t in tools:
                tool_names.append(t["name"])
            break

    print(f"total: {len(tool_names)}")
    for n in tool_names:
        print(f"  - {n}")

    return 0 if tool_names else 1


if __name__ == "__main__":
    raise SystemExit(main())
