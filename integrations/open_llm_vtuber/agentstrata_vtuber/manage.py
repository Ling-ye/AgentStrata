"""Standalone installer driver and launcher, invoked outside the Agent process."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen

from chatcopilot.core.install_artifacts import sha256
from .resources import artifact_lock, manifest, resource


def installation_checks(request: dict) -> dict:
    from .prepare import verify_receipt

    root = Path(request["root"])
    checks = {"python": sys.version_info[:2] == (3, 12)}
    lock = artifact_lock()
    for name, pin in manifest().items():
        directory = root / "runtime/apps" / name
        receipt_path = directory / ".agentstrata-source.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        checks[name] = bool(
            receipt and receipt.get("revision") == pin["revision"]
            and receipt.get("archive_sha256") == lock[name]["sha256"]
            and receipt.get("patch_sha256") == sha256(Path(str(resource(pin["patch"]))))
            and verify_receipt(directory, receipt))
    for name in ("node", "ffmpeg"):
        binary = root / "runtime/tools" / name / "bin" / name
        checks[name] = binary.is_file() and sha256(binary) == lock[name]["binary_sha256"]
    model_dir = root / "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    checks["asr"] = all((model_dir / v["file"]).is_file()
                       and sha256(model_dir / v["file"]) == v["sha256"] for v in lock["asr"])
    marker = root / "runtime/frontend-build.json"
    index = root / "runtime/apps/backend/frontend/index.html"
    checks["frontend_build"] = bool(marker.is_file() and index.is_file()
        and json.loads(marker.read_text()).get("index_sha256") == sha256(index))
    dependency_marker = root / "runtime/dependency-sha256"
    checks["dependencies"] = (dependency_marker.is_file()
        and dependency_marker.read_text() == sha256(Path(str(resource("requirements.lock")))))
    from chatcopilot.core.settings import load_local_env_values
    from .config import AdapterConfig
    from chatcopilot.botspec.loader import load_botspec

    environment = load_local_env_values(root / "config/runtime.env", expand_home=True)
    AdapterConfig.load(root / "config/adapter.json", environment)
    load_botspec(root / "config/bot.yaml")
    checks["configuration"] = bool(environment.get("CHATCOPILOT_SOURCE_URL"))
    from .assets import runtime_assets
    runtime_assets(root)
    checks["model_assets"] = True
    return {"ok": all(checks.values()), "checks": checks}


async def connection_checks(request: dict) -> dict:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from chatcopilot.core.settings import load_local_env_values
    from chatcopilot.protocols.gateway_client import GatewayWebSocketClient
    from .config import AdapterConfig

    root = Path(request["root"])
    environment = load_local_env_values(root / "config/runtime.env", expand_home=True)
    config = AdapterConfig.load(root / "config/adapter.json", environment)
    port = json.loads((root / "config/configured.json").read_text())["port"]
    client = GatewayWebSocketClient(config.gateway)
    checks = {}
    try:
        await client.connect()
        checks["gateway"] = True
    except Exception as exc:
        checks["gateway"] = type(exc).__name__
    finally:
        await client.close()
    try:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + config.mcp_token},
                                     timeout=15) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/agentstrata/mcp/",
                                               http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    checks["avatar_mcp"] = {t.name for t in tools.tools} == {
                        "list_actions", "set_expression", "play_motion"}
    except Exception as exc:
        checks["avatar_mcp"] = type(exc).__name__
    return checks


def doctor(request: dict) -> dict:
    result = installation_checks(request)
    result["checks"].update(asyncio.run(connection_checks(request)))
    from chatcopilot.core.settings import load_local_env_values
    environment = load_local_env_values(Path(request["root"]) / "config/runtime.env", expand_home=True)
    try:
        with urlopen(environment["CHATCOPILOT_SOURCE_URL"], timeout=30) as response:
            result["checks"]["source_accessible"] = response.status == 200 and bool(response.read(256))
    except Exception as exc:
        result["checks"]["source_accessible"] = type(exc).__name__
    result["ok"] = all(v is True for v in result["checks"].values())
    return result


def load_upstream(request: dict):
    """Resolve the installed upstream; never search an AgentStrata checkout."""
    from chatcopilot.core.settings import load_local_env_values
    root = Path(request["root"])
    from .assets import runtime_assets
    os.environ.update(load_local_env_values(root / "config/runtime.env", expand_home=True))
    os.environ.update(runtime_assets(root))
    backend = root / "runtime/apps/backend"
    os.chdir(backend)
    sys.path[:0] = [str(backend / "src"), str(backend)]
    os.environ["HF_HOME"] = str(root / "data/models")
    os.environ["MODELSCOPE_CACHE"] = str(root / "data/models")
    from open_llm_vtuber.config_manager import read_yaml, validate_config
    return validate_config(read_yaml(str(root / "config/upstream.yaml")))


async def serve(request: dict) -> None:
    config = load_upstream(request)
    from loguru import logger
    import uvicorn
    from open_llm_vtuber.server import WebSocketServer

    logger.remove()
    logger.add(sys.stderr, level="INFO", diagnose=False)
    server = WebSocketServer(config=config)
    await server.initialize()
    await uvicorn.Server(uvicorn.Config(server.app, host=config.system_config.host,
                                        port=config.system_config.port, log_level="info")).serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentstrata-vtuber")
    parser.add_argument("operation", choices=("prepare", "check", "doctor", "run"))
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    request = json.loads(args.request.read_text())
    try:
        supplied = request["options"]
        defaults = {"port": 12393, "gateway_port": 18791, "runtime": "codex"}
        if set(supplied) - set(defaults):
            raise ValueError("VTuber options: port, gateway_port, runtime")
        request["options"] = {**defaults, **supplied}
        if request["options"]["runtime"] not in {"codex", "langgraph"}:
            raise ValueError("Unsupported Agent runtime")
        if args.operation == "run":
            asyncio.run(serve(request))
            return 0
        if args.operation == "prepare":
            from .prepare import prepare
            result = prepare(request)
        elif args.operation == "check":
            result = installation_checks(request)
        else:
            result = doctor(request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Private stage logs retain the cause without printing environments/credentials.
        print(f"{args.operation}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
