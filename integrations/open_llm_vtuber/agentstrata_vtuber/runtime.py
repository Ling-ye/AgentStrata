"""Client process composition. The control endpoint does not connect to the Gateway."""
from __future__ import annotations

import os
import logging

from .avatar import BrowserAvatarController, record_playback
from .config import AdapterConfig
from .control import install_control

_services: tuple[AdapterConfig, BrowserAvatarController] | None = None


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
    install_control(app, config, controller)


def attach(client_id: str, model_name: str, send) -> None:
    config, controller = services()
    if config.mcp_token is not None:
        controller.attach(client_id, model_name, send)


def detach(client_id: str) -> None:
    services()[1].detach(client_id)


def receive(client_id: str, payload: dict) -> None:
    if payload.get("type") == "agentstrata-avatar-result":
        services()[1].acknowledge(client_id, payload)
    elif payload.get("type") == "agentstrata-playback":
        record_playback(client_id, payload)
