"""Round-trip YAML editor for bot tool configuration.

Uses ruamel.yaml to preserve comments, ordering, and formatting when
updating bot.yaml and mcp/servers.yaml from the console UI.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.default_flow_style = False


def _backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


def _load(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return _yaml.load(f)


def _save(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        _yaml.dump(data, f)


# ---------------------------------------------------------------------------
# bot.yaml tools
# ---------------------------------------------------------------------------

def update_bot_tools(
    bot_yaml_path: Path,
    packs: list[str],
    features: list[str],
    hide: list[str],
) -> None:
    """Update tools.packs / tools.features / tools.hide in bot.yaml."""
    _backup(bot_yaml_path)
    data = _load(bot_yaml_path)
    if data is None:
        data = {}

    if "tools" not in data:
        data["tools"] = {}
    tools = data["tools"]

    tools["packs"] = packs
    if features:
        tools["features"] = features
    elif "features" in tools:
        del tools["features"]
    if hide:
        tools["hide"] = hide
    elif "hide" in tools:
        del tools["hide"]

    _save(bot_yaml_path, data)


# ---------------------------------------------------------------------------
# mcp/servers.yaml
# ---------------------------------------------------------------------------

def update_mcp_servers(
    servers_yaml_path: Path,
    server_refs: list[dict[str, Any]],
) -> None:
    """Update the servers list in mcp/servers.yaml."""
    servers_yaml_path.parent.mkdir(parents=True, exist_ok=True)

    if servers_yaml_path.is_file():
        _backup(servers_yaml_path)
        data = _load(servers_yaml_path)
    else:
        data = {}

    if data is None:
        data = {}

    entries = []
    for ref_item in server_refs:
        entry: dict[str, Any] = {"ref": ref_item["ref"]}
        enabled = ref_item.get("enabled", True)
        if not enabled:
            entry["enabled"] = False
        else:
            entry["enabled"] = True
        entries.append(entry)

    data["servers"] = entries
    _save(servers_yaml_path, data)


def _ensure_mcp_section(bot_yaml_path: Path) -> None:
    """Ensure bot.yaml has tools.mcp.servers pointing to mcp/servers.yaml."""
    data = _load(bot_yaml_path)
    if data is None:
        data = {}

    tools = data.get("tools")
    if not isinstance(tools, dict):
        data["tools"] = {}
        tools = data["tools"]
    mcp = tools.get("mcp")
    if not isinstance(mcp, dict):
        tools["mcp"] = {"servers": "mcp/servers.yaml"}
    elif not mcp.get("servers"):
        mcp["servers"] = "mcp/servers.yaml"
    else:
        return  # already configured

    _save(bot_yaml_path, data)


def _has_mcp_section(bot_yaml_path: Path) -> bool:
    data = _load(bot_yaml_path)
    if not isinstance(data, dict):
        return False
    tools = data.get("tools")
    if not isinstance(tools, dict):
        return False
    return isinstance(tools.get("mcp"), dict)


# ---------------------------------------------------------------------------
# bot.yaml agents
# ---------------------------------------------------------------------------

def update_subagents(
    bot_yaml_path: Path,
    include: list[str],
    workflows: list[str],
) -> None:
    """Update agents.presets and agents.workflows in bot.yaml."""
    _backup(bot_yaml_path)
    data = _load(bot_yaml_path)
    if data is None:
        data = {}

    if "agents" not in data:
        data["agents"] = {}
    sa = data["agents"]

    sa["presets"] = include
    if workflows:
        sa["workflows"] = workflows
    elif "workflows" in sa:
        del sa["workflows"]

    _save(bot_yaml_path, data)


# ---------------------------------------------------------------------------
# Unified update
# ---------------------------------------------------------------------------

def apply_tool_config(
    bot_yaml_path: Path,
    *,
    tool_packs: list[str] | None = None,
    hidden_tools: list[str] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    agent_presets: list[str] | None = None,
    workflows: list[str] | None = None,
    tool_features: list[str] | None = None,
) -> dict[str, Any]:
    """Apply a full tool configuration update.

    Returns ``{ok, files_modified, warnings}``.
    """
    files_modified: list[str] = []
    warnings: list[str] = []

    update_bot_tools(
        bot_yaml_path,
        packs=tool_packs or [],
        features=tool_features or [],
        hide=hidden_tools or [],
    )
    files_modified.append(str(bot_yaml_path))

    if mcp_servers is not None:
        servers_yaml_path = bot_yaml_path.parent / "mcp" / "servers.yaml"
        should_write_mcp = bool(mcp_servers) or servers_yaml_path.exists() or _has_mcp_section(bot_yaml_path)
        if should_write_mcp:
            _ensure_mcp_section(bot_yaml_path)
            update_mcp_servers(servers_yaml_path, mcp_servers)
            files_modified.append(str(servers_yaml_path))

    update_subagents(bot_yaml_path, agent_presets or [], workflows or [])

    return {"ok": True, "files_modified": files_modified, "warnings": warnings}
