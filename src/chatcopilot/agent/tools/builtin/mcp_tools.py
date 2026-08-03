"""Compatibility re-export for MCP admin tools.

Canonical implementation lives in :mod:`chatcopilot.external_tools.mcp_admin.tools`
so the Agent package does not import BotSpec configuration modules.
"""
from __future__ import annotations

from chatcopilot.external_tools.mcp_admin.tools import TOOLS

__all__ = ["TOOLS"]
