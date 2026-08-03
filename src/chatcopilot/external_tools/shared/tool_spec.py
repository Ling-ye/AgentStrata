"""Compatibility import path for tool contracts.

The canonical ToolDef/ToolContext contract lives in ``chatcopilot.contracts.tools``.
External tool packages keep importing this module during the migration, but the
single source of truth is now the contracts package.
"""
from __future__ import annotations

from chatcopilot.contracts.tools import *  # noqa: F403
from chatcopilot.contracts.tools import __all__  # noqa: F401
