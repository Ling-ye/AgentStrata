"""Top-level AgentStrata command line entry."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        _print_help()
        return 0

    command = args.pop(0)
    if command == "botspec":
        from chatcopilot.botspec.__main__ import main as botspec_main

        return botspec_main(args)
    if command == "bot":
        from chatcopilot.botspec.cli import main as bot_main

        return bot_main(args)
    if command == "run":
        from chatcopilot.run import main as run_main

        return run_main(args)
    if command == "acp-edge":
        if args:
            parser = argparse.ArgumentParser(prog="python -m chatcopilot acp-edge")
            parser.error(f"unexpected arguments: {' '.join(args)}")
        from chatcopilot.protocols.acp import main_from_env

        return main_from_env()
    if command == "mcp-server":
        if args:
            parser = argparse.ArgumentParser(prog="python -m chatcopilot mcp-server")
            parser.error(f"unexpected arguments: {' '.join(args)}")
        from chatcopilot.middleware.mcp.server import serve as mcp_server_main

        return mcp_server_main()
    if command == "mcp-session-gateway":
        parser = argparse.ArgumentParser(
            prog="python -m chatcopilot mcp-session-gateway"
        )
        parser.add_argument("config")
        parsed = parser.parse_args(args)
        from chatcopilot.middleware.mcp.session_gateway import serve

        return serve(parsed.config)
    if command == "evals":
        from chatcopilot.evals.cli import main as evals_main

        return evals_main(args)
    if command == "http-api-server":
        from chatcopilot.middleware.http.server import main as http_api_main

        return http_api_main(args)
    if command == "qq-at-proxy":
        from chatcopilot.platforms.qq.at_proxy import main as qq_at_proxy_main

        return qq_at_proxy_main(args)

    parser = argparse.ArgumentParser(prog="python -m chatcopilot")
    parser.error(f"unknown command: {command}")
    return 2


def _print_help() -> None:
    print(
        "AgentStrata commands:\n"
        "  agentstrata botspec validate bots/<bot-id>/bot.yaml\n"
        "  agentstrata botspec show bots/<bot-id>/bot.yaml\n"
        "  agentstrata bot list\n"
        "  agentstrata bot new <id> --platform feishu\n"
        "  agentstrata bot doctor --bot bots/<bot-id>/bot.yaml\n"
        "  agentstrata bot external-check --bot bots/<bot-id>/bot.yaml --json\n"
        "  agentstrata bot codex-auth login --bot bots/<bot-id>/bot.yaml --lane all\n"
        "  agentstrata bot codex-auth status --bot bots/<bot-id>/bot.yaml --lane all --json\n"
        "  agentstrata bot route-explain --bot bots/<bot-id>/bot.yaml \"modify Dockerfile\"\n"
        "  agentstrata bot provision-env --bot bots/<bot-id>/bot.yaml\n"
        "  agentstrata bot render-cc-config --bot bots/<bot-id>/bot.yaml --out config.toml\n"
        "  agentstrata bot render-session-env --bot bots/<bot-id>/bot.yaml --session-key <key>\n"
        "  agentstrata run --bot bots/<bot-id>/bot.yaml\n"
        "  agentstrata acp-edge\n"
        "  agentstrata mcp-server\n"
        "  agentstrata mcp-session-gateway <session-config.json>\n"
        "  agentstrata evals list\n"
        "  agentstrata evals run --suite ifeval --bot bots/<bot-id>/bot.yaml "
        "--output reports/evals/manual/ifeval-run\n"
        "  agentstrata evals run --profile agent-comparison-mvp "
        "--bot bots/<bot-id>/bot.yaml --output reports/evals/manual/agent-comparison\n"
        "  agentstrata evals prepare --suite ifeval\n"
        "  agentstrata http-api-server --host 127.0.0.1 --port 8787\n"
        "  agentstrata qq-at-proxy\n"
        "\n"
        "Compatibility entry point: python -m chatcopilot <command>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
