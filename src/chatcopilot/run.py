"""Run one BotSpec-defined AgentStrata instance through its declared host."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from chatcopilot.botspec import (
    BotRuntimeContext,
    assemble_runtime_context,
    load_botspec,
    resolve_bot_spec_path,
)
from chatcopilot.botspec.runtime_env import apply_runtime_env
from chatcopilot.core.settings import set_bot_spec_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chatcopilot run",
        description="Run an AgentStrata bot from bots/<bot-id>/bot.yaml.",
    )
    parser.add_argument(
        "--bot",
        required=True,
        help="Bot id such as lingye-copilot-qq, or path to a bot.yaml file.",
    )
    parser.add_argument(
        "--transport",
        default="acp",
        choices=("acp",),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    bot_path = resolve_bot_spec_path(Path(args.bot) if _looks_like_path(args.bot) else args.bot)
    runtime = assemble_runtime_context(load_botspec(bot_path))
    set_bot_spec_env(runtime.source_path)
    apply_runtime_env(runtime)
    if runtime.gateway is not None and runtime.channels.qq is not None:
        return _run_gateway(runtime)
    _start_codebase_index_warmup(runtime)
    return _run_legacy_acp(runtime)


def _run_gateway(runtime: BotRuntimeContext) -> int:
    from chatcopilot.gateway.runtime import main as gateway_main

    return gateway_main(
        runtime,
        after_build=lambda: _start_codebase_index_warmup(runtime),
    )


def _run_legacy_acp(runtime: BotRuntimeContext) -> int:
    """Keep non-Gateway platforms on the isolated legacy ACP host."""

    from chatcopilot.middleware.acp.server import main as acp_main

    return acp_main(runtime)


def _looks_like_path(value: str) -> bool:
    return any(sep in value for sep in ("/", "\\")) or value.endswith((".yaml", ".yml"))


def _start_codebase_index_warmup(runtime: BotRuntimeContext) -> None:
    if "codebase.read" not in runtime.tool_packs or not runtime.spec.context.codebases.registry:
        return

    def _warm() -> None:
        try:
            from chatcopilot.external_tools.codebase.config import load_registry
            from chatcopilot.external_tools.codebase.index import refresh_index

            for repository in load_registry(force_reload=True).repositories.values():
                refresh_index(repository)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("codebase index warmup failed: %s", exc)

    threading.Thread(
        target=_warm,
        name=f"codebase-index-{runtime.bot_id}",
        daemon=True,
    ).start()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
