"""Client process composition. The control endpoint does not connect to the Gateway."""
from __future__ import annotations

import os
import logging
import asyncio
import json
from dataclasses import replace
from uuid import uuid4

from .avatar import BrowserAvatarController, record_playback
from .config import AdapterConfig
from .control import install_control

_services: tuple[AdapterConfig, BrowserAvatarController] | None = None
_observer = None
_observer_client_id: str | None = None


def observer():
    global _observer
    token = os.environ.get("AGENTSTRATA_AVATAR_OBSERVER_TOKEN")
    if _observer is None and token:
        from chatcopilot.protocols.gateway_client import GatewayWebSocketClient
        from .bridge import ConversationBridge
        from .perception import DesktopObserver
        config = replace(services()[0].gateway, client_id="avatar-observer", token=token)
        _observer = DesktopObserver(ConversationBridge(GatewayWebSocketClient(config), client_id=config.client_id))
    return _observer


def services() -> tuple[AdapterConfig, BrowserAvatarController]:
    global _services
    if _services is None:
        config = AdapterConfig.load(os.environ["AGENTSTRATA_VTUBER_CONFIG"])
        _services = config, BrowserAvatarController(config)
    return _services


def install(app, *, host: str) -> None:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("AgentStrata avatar integration requires a loopback listener")
    logger = logging.getLogger("agentstrata_vtuber")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    config, controller = services()
    install_control(app, config, controller, observer=observer())
    if os.environ.get("AGENTSTRATA_APPLICATION_ROOT"):
        from pathlib import Path
        from fastapi.responses import FileResponse
        from .source_delivery import source_archive
        root = Path(os.environ["AGENTSTRATA_APPLICATION_ROOT"])
        @app.get("/agentstrata/source.zip")
        def corresponding_source():
            return FileResponse(source_archive(root), media_type="application/zip", filename="AgentStrata-source.zip")


def attach(client_id: str, model_name: str, send, context=None) -> None:
    global _observer_client_id
    config, controller = services()
    if config.mcp_token is not None:
        controller.attach(client_id, model_name, send)
    watcher = observer()
    if watcher is not None and context is not None:
        async def present(text: str, session_id: str, run_id: str):
            from open_llm_vtuber.agent.output_types import DisplayText
            from open_llm_vtuber.utils.stream_audio import prepare_audio_payload
            await send(json.dumps({"type": "agentstrata-reply", "session_id": session_id, "run_id": run_id}))
            await send(json.dumps({"type": "desktop-notice", "text": text}))
            audio_path = None
            try:
                audio_path = await context.tts_engine.async_generate_audio(text=text, file_name_no_ext="notice-" + uuid4().hex)
                payload = await asyncio.to_thread(prepare_audio_payload, audio_path,
                    display_text=DisplayText(text=text, name=context.character_config.character_name))
                await send(json.dumps(payload))
                await send(json.dumps({"type": "backend-synth-complete"}))
            finally:
                if audio_path:
                    context.tts_engine.remove_file(audio_path)
        watcher.present = present
        _observer_client_id = client_id


def detach(client_id: str) -> None:
    global _observer_client_id
    services()[1].detach(client_id)
    watcher = observer()
    if watcher is not None and client_id == _observer_client_id:
        _observer_client_id = None
        watcher.present = None
        asyncio.create_task(watcher.update(False))


def receive(client_id: str, payload: dict) -> None:
    if payload.get("type") == "agentstrata-avatar-result":
        services()[1].acknowledge(client_id, payload)
    elif payload.get("type") == "agentstrata-playback":
        record_playback(client_id, payload)
