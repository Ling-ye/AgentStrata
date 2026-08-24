"""Authenticated loopback relay for explicit QQ group mentions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hmac
import json
import logging
import os
from typing import Any, Mapping
from urllib.parse import urlsplit

from chatcopilot.core.logging import configure_logging
from chatcopilot.core.allowlists import is_numeric_platform_id
from chatcopilot.platforms.qq.boundary import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)
from chatcopilot.project import ENV_PREFIX

_LOGGER = logging.getLogger("chatcopilot.platforms.qq.at_proxy")
_DEFAULT_LISTEN = "ws://127.0.0.1:3002"
_DEFAULT_UPSTREAM = "ws://127.0.0.1:3001"


@dataclass(frozen=True)
class ForwardDecision:
    forward: bool
    code: str
    chat_kind: str = ""


def _has_explicit_self_at(event: Mapping[str, Any], bot_qq: str) -> bool:
    message = event.get("message")
    if not isinstance(message, list):
        return False
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "at":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if str(data.get("qq", "")).strip() == bot_qq:
            return True
    return False


def evaluate_forward(event: Any, bot_qq: str) -> ForwardDecision:
    """Apply only the fixed transport trigger for QQ message events."""

    if not isinstance(event, dict):
        return ForwardDecision(True, "non_object_passthrough")
    if event.get("post_type") != "message":
        return ForwardDecision(True, "non_message_passthrough")

    message_type = str(event.get("message_type") or "").strip()
    if message_type == "private":
        return ForwardDecision(True, "private_passthrough", chat_kind="p2p")
    if message_type != "group":
        return ForwardDecision(False, "unsupported_message_type", chat_kind=message_type)
    if not bot_qq:
        return ForwardDecision(False, "bot_identity_missing", chat_kind="group")

    matched = _has_explicit_self_at(event, bot_qq)
    return ForwardDecision(
        matched,
        "group_mention_matched" if matched else "group_mention_missing",
        chat_kind="group",
    )


class RelayConfig:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        values = os.environ if env is None else env
        self.listen_url = (values.get("QQ_AT_PROXY_URL") or _DEFAULT_LISTEN).strip()
        self.upstream_url = (values.get("QQ_WS_URL") or _DEFAULT_UPSTREAM).strip()
        self.token = (values.get("QQ_ACCESS_TOKEN") or "").strip()
        self.bot_qq = (values.get("QQ_ACCOUNT") or "").strip()

    @property
    def listen_host_port(self) -> tuple[str, int]:
        parts = urlsplit(self.listen_url)
        return parts.hostname or "127.0.0.1", parts.port or 3002


def validate_relay_config(config: RelayConfig) -> None:
    require_loopback_websocket_url(config.listen_url, env_key="QQ_AT_PROXY_URL")
    require_loopback_websocket_url(config.upstream_url, env_key="QQ_WS_URL")
    require_access_token(config.token)
    if not is_numeric_platform_id(config.bot_qq):
        raise QQBoundaryError(
            "qq_account_invalid",
            "QQ_ACCOUNT must be a numeric QQ account for the QQ group mention relay",
        )


def _authorization_header(connection: Any) -> str:
    request = getattr(connection, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        headers = getattr(connection, "request_headers", None)
    if headers is None:
        return ""
    return str(headers.get("Authorization") or "")


def _downstream_authenticated(connection: Any, token: str) -> bool:
    return hmac.compare_digest(
        _authorization_header(connection),
        f"Bearer {token}",
    )


async def _connect_upstream(config: RelayConfig) -> Any:
    import websockets

    headers = [("Authorization", f"Bearer {config.token}")]
    try:
        return await websockets.connect(
            config.upstream_url, additional_headers=headers, max_size=None
        )
    except TypeError:
        return await websockets.connect(config.upstream_url, extra_headers=headers, max_size=None)


async def _pump_downstream(napcat_ws: Any, cc_ws: Any, config: RelayConfig) -> None:
    async for raw in napcat_ws:
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            await cc_ws.send(raw)
            continue
        decision = evaluate_forward(event, config.bot_qq)
        if decision.forward:
            await cc_ws.send(raw)
            continue
        _LOGGER.info(
            "drop QQ message by mention relay | kind=%s reason=%s",
            decision.chat_kind or "unknown",
            decision.code,
        )


async def _pump_upstream(cc_ws: Any, napcat_ws: Any) -> None:
    async for raw in cc_ws:
        await napcat_ws.send(raw)


async def handle_downstream_connection(cc_ws: Any, config: RelayConfig) -> None:
    import websockets

    if not _downstream_authenticated(cc_ws, config.token):
        _LOGGER.warning("rejected unauthenticated cc-connect relay connection")
        await cc_ws.close(code=1008, reason="authentication required")
        return
    try:
        napcat_ws = await _connect_upstream(config)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("upstream connect failed (%s): %s", config.upstream_url, exc)
        await cc_ws.close()
        return
    _LOGGER.info("cc-connect attached; upstream %s connected", config.upstream_url)
    try:
        async with napcat_ws:
            downstream = asyncio.create_task(_pump_downstream(napcat_ws, cc_ws, config))
            upstream = asyncio.create_task(_pump_upstream(cc_ws, napcat_ws))
            _done, pending = await asyncio.wait(
                {downstream, upstream}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except websockets.ConnectionClosed:
        pass
    except Exception:  # noqa: BLE001
        _LOGGER.exception("relay error")
    finally:
        _LOGGER.info("cc-connect detached")


async def serve_relay(config: RelayConfig) -> None:
    import websockets

    host, port = config.listen_host_port

    async def handler(cc_ws: Any, *_: Any) -> None:
        await handle_downstream_connection(cc_ws, config)

    _LOGGER.info(
        "QQ mention relay listening on ws://%s:%d -> upstream %s",
        host,
        port,
        config.upstream_url,
    )
    async with websockets.serve(handler, host, port, max_size=None):
        await asyncio.Future()


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO", f"{ENV_PREFIX}_ACP_LOG_LEVEL")
    try:
        import websockets  # noqa: F401
    except ImportError:
        _LOGGER.error(
            "缺少 websockets 依赖，QQ @ Relay 无法启动；请把 websockets 装进实例 venv 后重试。"
        )
        return 1
    config = RelayConfig()
    try:
        validate_relay_config(config)
    except QQBoundaryError as exc:
        _LOGGER.error("QQ @ relay boundary rejected | code=%s error=%s", exc.error_code, exc)
        return 2
    try:
        asyncio.run(serve_relay(config))
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = [
    "ForwardDecision",
    "RelayConfig",
    "evaluate_forward",
    "handle_downstream_connection",
    "main",
    "serve_relay",
    "validate_relay_config",
]
