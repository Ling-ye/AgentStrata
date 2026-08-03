"""Compatibility entrypoint for the standalone MCP search probe.

The implementation lives in :mod:`chatcopilot.search_probe` because it loads
BotSpec configuration and is not part of the Agent layer.
"""
from __future__ import annotations

from chatcopilot.search_probe import ProbeResult, main, run_probes

__all__ = ["ProbeResult", "main", "run_probes"]


if __name__ == "__main__":
    raise SystemExit(main())
