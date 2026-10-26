#!/usr/bin/env python3
"""Shared local/CI check: installed plugin, pinned sources, build and real HTTP/WS."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import wave


ROOT = Path(__file__).resolve().parents[1]


async def probe(request: dict) -> None:
    """Runs in the isolated installed environment; substitutes only speech/Agent."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import uvicorn
    import websockets
    import agentstrata_vtuber
    from agentstrata_vtuber.prepare import prepare_dependencies, prepare_sources, prepare_tools, prepare_frontend
    from agentstrata_vtuber.deployment import configure
    from agentstrata_vtuber.manage import load_upstream
    from chatcopilot.core.install_artifacts import write_private

    root = Path(request["root"])
    assert Path(agentstrata_vtuber.__file__).is_relative_to(root / "runtime/venv")
    prepare_dependencies(request)
    sources = prepare_sources(root)
    node, npm = prepare_tools(root)
    prepare_frontend(root, sources, node, npm)
    write_private(root / "config/model.json", json.dumps({
        "connections": {"fixture": {"kind": "openai_compatible", "auth": {
            "mode": "api_key", "key_env": "FIXTURE_UNUSED_API_KEY"}}},
        "profiles": {"fixture": {"connection": "fixture", "model": "fixture"}},
        "bindings": {"chat": "fixture"}}))
    configure(request, sources)
    config = load_upstream(request)
    from agentstrata_vtuber import agent, runtime
    from agentstrata_vtuber.bridge import Reply
    from open_llm_vtuber.asr.asr_factory import ASRFactory
    from open_llm_vtuber.tts.tts_factory import TTSFactory
    from open_llm_vtuber.tts.tts_interface import TTSInterface
    from open_llm_vtuber.vad.vad_factory import VADFactory
    from open_llm_vtuber.server import WebSocketServer

    class SpeechInput:
        async def async_transcribe_np(self, audio):
            return "recorded fixture"

    class SpeechOutput(TTSInterface):
        def generate_audio(self, text, file_name_no_ext=None):
            path = self.generate_cache_file_name(file_name_no_ext)
            with wave.open(path, "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"".join(struct.pack("<h", int(1000 * math.sin(i / 8)))
                                           for i in range(1600)))
            return path

    class Dialogue:
        def __init__(self, *args, **kwargs):
            self.session = ""
        def select_history(self, character, history):
            self.session = character + history
        async def chat(self, text):
            if text == "action":
                assert (await runtime.services()[1].set_expression("happy"))["status"] == "executed"
                assert (await runtime.services()[1].play_motion("gesture"))["status"] == "executed"
            return Reply(self.session, "fixture-run",
                         "fixture reply " + text + "\nSource: https://example.com/source", "completed")
        async def cancel(self):
            pass
        async def close(self):
            pass

    ASRFactory.get_asr_system = staticmethod(lambda *args, **kwargs: SpeechInput())
    TTSFactory.get_tts_engine = staticmethod(lambda *args, **kwargs: SpeechOutput())
    VADFactory.get_vad_engine = staticmethod(lambda *args, **kwargs: None)
    agent.ConversationBridge = Dialogue
    app = WebSocketServer(config)
    await app.initialize()
    port = request["options"]["port"]
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if serving.done():
                await serving
                raise AssertionError("server exited before startup")
            await asyncio.sleep(0.1)
        assert server.started
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"http://127.0.0.1:{port}/")).status_code == 200
            assert (await http.get(f"http://127.0.0.1:{port}/", headers={
                "Origin": "https://attacker.invalid"})).status_code == 403
            assert (await http.post(f"http://127.0.0.1:{port}/agentstrata/mcp/")).status_code == 403
        # A real websocket keeps accepting action callbacks while dialogue awaits its tool.
        for turn in ("action", "/source", "voice"):
            async with websockets.connect(f"ws://127.0.0.1:{port}/client-ws",
                    origin=f"http://localhost:{port}") as ws:
                while json.loads(await asyncio.wait_for(ws.recv(), 10)).get("type") != "agentstrata-mode":
                    pass
                await ws.send(json.dumps({"type": "create-new-history"}))
                while json.loads(await asyncio.wait_for(ws.recv(), 10)).get("type") != "new-history-created":
                    pass
                if turn == "voice":
                    await ws.send(json.dumps({"type": "mic-audio-data", "audio": [0.1] * 1600}))
                    await ws.send(json.dumps({"type": "mic-audio-end"}))
                else:
                    await ws.send(json.dumps({"type": "text-input", "text": turn}))
                actions, displayed = 0, ""
                for _ in range(30):
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 20))
                    if frame["type"] == "agentstrata-avatar-action":
                        actions += 1
                        await ws.send(json.dumps({"type": "agentstrata-avatar-result",
                            "request_id": frame["request_id"], "status": "executed"}))
                    if frame["type"] == "audio":
                        displayed += frame.get("display_text", {}).get("text", "")
                    if frame["type"] == "backend-synth-complete":
                        await ws.send(json.dumps({"type": "frontend-playback-complete"}))
                    if frame.get("text") == "conversation-chain-end":
                        break
                assert "Source: https://example.com/source" in displayed
                assert ("recorded fixture" if turn == "voice" else turn) in displayed
                assert actions == (2 if turn == "action" else 0)
                await ws.send(json.dumps({"type": "interrupt-signal", "text": ""}))
        token = runtime.services()[0].mcp_token
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/agentstrata/mcp/",
                                               http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert {v.name for v in (await session.list_tools()).tools} == {
                        "list_actions", "set_expression", "play_motion"}
                    result = await session.call_tool("play_motion", {"name": "gesture"})
                    assert json.loads(result.content[0].text)["status"] == "unavailable"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
    print("Installed plugin: pinned patches, frontend build, HTTP/Origin, websocket, callbacks, reconnect and MCP passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--probe", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=19493)
    args = parser.parse_args()
    if args.probe:
        # The fresh venv initially has only the two distribution wheels.
        from agentstrata_vtuber.prepare import prepare_dependencies
        request = json.loads(args.probe.read_text())
        prepare_dependencies(request)
        asyncio.run(probe(request))
        return 0
    if sys.version_info[:2] != (3, 12):
        parser.error("Run this compatibility check with Python 3.12")
    sys.path.insert(0, str(ROOT / "src"))
    from chatcopilot.core.install_artifacts import dependency_environment, write_private
    from chatcopilot.plugins.catalog import PLUGINS
    from chatcopilot.plugins.runtime import bootstrap, ensure_uv
    from plugin_artifacts import build
    with tempfile.TemporaryDirectory(prefix="as-vtuber-compat-") as temp:
        work = (args.work_dir or Path(temp)).resolve()
        work.mkdir(parents=True, exist_ok=True)
        artifacts = work / "artifacts"
        artifacts.mkdir(exist_ok=True)
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
                        "--outdir", str(artifacts)], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL)
        plugin, _ = build(ROOT / "integrations/open_llm_vtuber", artifacts)
        host = next(artifacts.glob("agentstrata-*.whl"))
        root = work / "installation"
        uv = ensure_uv(work)
        python = bootstrap(PLUGINS["open-llm-vtuber"], root, (host, plugin), uv)
        request = {"root": str(root), "uv": str(uv),
            "host": {"python": str(python), "source_url": "https://example.com/source",
                     "model_config": str(root / "config/model.json"), "model_binding": "chat"},
            "options": {"port": args.port, "gateway_port": args.port + 1, "runtime": "langgraph"}}
        request_path = root / "config/request.json"
        write_private(request_path, json.dumps(request))
        # This script is also copied: the child cannot import checkout modules.
        copied = work / "probe.py"
        shutil.copyfile(__file__, copied)
        subprocess.run([str(python), str(copied), "--probe", str(request_path)],
                       cwd=work, env=dependency_environment(), check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
