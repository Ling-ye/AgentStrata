"""Hermetic current-Channel ingress probe using a synthetic OneBot provider.

This proves local authenticated intake and mention filtering, not real QQ delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from chatcopilot.channels.qq_onebot import OneBotChannelConfig, OneBotForwardWebSocketDriver
from chatcopilot.channels.qq_onebot.codec import has_structured_self_mention
from chatcopilot.contracts.gateway import CanonicalInboundEvent

_LOOPBACK_HOST = "127.0.0.1"
_MAX_PROBE_FRAME_BYTES = 64 * 1024
_FIRST_FRAME_TIMEOUT_SECONDS = 2.0
_EXTRA_FRAME_TIMEOUT_SECONDS = 0.15


@dataclass(frozen=True)
class SimulatedGatewayIngressResult:
    """Secret-free evidence from one hermetic gateway relay run."""

    upstream_authenticated: bool
    positive_forwarded: bool
    negative_dropped: bool
    positive_frame_sha256: str
    negative_frame_sha256: str
    mode: str = "hermetic_loopback"

    @property
    def passed(self) -> bool:
        return self.upstream_authenticated and self.positive_forwarded and self.negative_dropped

    def to_evidence(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "upstream_authenticated": self.upstream_authenticated,
            "positive_forwarded": self.positive_forwarded,
            "negative_dropped": self.negative_dropped,
            "positive_frame_sha256": self.positive_frame_sha256,
            "negative_frame_sha256": self.negative_frame_sha256,
        }


def _random_numeric_id(*, excluded: frozenset[str]) -> str:
    for _ in range(32):
        candidate = str(secrets.randbelow(8_000_000_000) + 1_000_000_000)
        if candidate not in excluded:
            return candidate
    raise RuntimeError("unable to allocate a synthetic gateway identity")


def _synthetic_probe_env() -> dict[str, str]:
    synthetic_bot = _random_numeric_id(excluded=frozenset())
    return {
        "QQ_ACCESS_TOKEN": secrets.token_urlsafe(32),
        "QQ_ACCOUNT": synthetic_bot,
        "QQ_WS_URL": "ws://127.0.0.1:1",
    }


def _probe_events(cfg: OneBotChannelConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    excluded = frozenset({cfg.account_id})
    user_id = _random_numeric_id(excluded=excluded)
    group_id = _random_numeric_id(excluded=excluded | {user_id})

    nonce = secrets.token_hex(8)
    positive: dict[str, Any] = {
        "time": 0,
        "self_id": int(cfg.account_id),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": secrets.randbelow(2_000_000_000) + 1,
        "group_id": int(group_id),
        "user_id": int(user_id),
        "message": [
            {"type": "at", "data": {"qq": cfg.account_id}},
            {"type": "text", "data": {"text": f" ingress-probe-{nonce}"}},
        ],
        "raw_message": f"[CQ:at,qq={cfg.account_id}] ingress-probe-{nonce}",
        "sender": {"user_id": int(user_id)},
    }
    negative = dict(positive)
    negative["message_id"] = secrets.randbelow(2_000_000_000) + 1
    negative["message"] = [
        {"type": "text", "data": {"text": f"ingress-deny-{nonce}"}},
    ]
    negative["raw_message"] = f"ingress-deny-{nonce}"

    if not has_structured_self_mention(positive["message"], cfg.account_id):
        raise RuntimeError("Channel rejected the synthetic positive event")
    if has_structured_self_mention(negative["message"], cfg.account_id):
        raise RuntimeError("Channel accepted the synthetic negative event")
    return positive, negative


def _server_port(server: Any) -> int:
    sockets = tuple(server.sockets or ())
    if len(sockets) != 1:
        raise RuntimeError("gateway ingress probe did not receive one listener")
    return int(sockets[0].getsockname()[1])


def _authorization_header(connection: Any) -> str:
    request = getattr(connection, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        headers = getattr(connection, "request_headers", None)
    if headers is None:
        return ""
    return str(headers.get("Authorization") or "")


async def run_simulated_gateway_ingress(
    env: Mapping[str, str],
    *,
    downstream_observer: Callable[[Mapping[str, Any]], Awaitable[None] | None] | None = None,
) -> SimulatedGatewayIngressResult:
    """Exercise the same authenticated Channel driver used by Gateway."""
    import websockets

    def config(values: Mapping[str, str], url: str = "ws://127.0.0.1:1") -> OneBotChannelConfig:
        return OneBotChannelConfig(channel_id="ingress-probe", account_id=values.get("QQ_ACCOUNT", ""),
                                   access_token=values.get("QQ_ACCESS_TOKEN", ""), websocket_url=url)

    config(env)
    synthetic_env = _synthetic_probe_env()
    synthetic = config(synthetic_env)
    positive, negative = _probe_events(synthetic)
    positive_raw = json.dumps(positive, ensure_ascii=True, separators=(",", ":"))
    negative_raw = json.dumps(negative, ensure_ascii=True, separators=(",", ":"))
    release = asyncio.Event()
    observed = asyncio.Event()
    authenticated = False
    received: list[str] = []

    async def fake_napcat(connection: Any) -> None:
        nonlocal authenticated
        authenticated = hmac.compare_digest(_authorization_header(connection), f"Bearer {synthetic.access_token}")
        if not authenticated:
            await connection.close(code=1008, reason="authentication required")
            return
        request = json.loads(await connection.recv())
        if request.get("action") != "get_login_info":
            return
        await connection.send(json.dumps({"status": "ok", "retcode": 0,
                                          "data": {"user_id": synthetic.account_id}, "echo": request["echo"]}))
        await connection.send(negative_raw)
        await connection.send(positive_raw)
        await release.wait()

    async def accept(event: CanonicalInboundEvent) -> None:
        received.append(str(event.evidence.message_id))
        if event.evidence.message_id == str(positive["message_id"]):
            if downstream_observer is not None:
                result = downstream_observer(dict(positive))
                if result is not None:
                    await result
            observed.set()

    async with websockets.serve(fake_napcat, _LOOPBACK_HOST, 0, compression=None,
                                max_size=_MAX_PROBE_FRAME_BYTES) as server:
        driver = OneBotForwardWebSocketDriver(
            config(synthetic_env, f"ws://{_LOOPBACK_HOST}:{_server_port(server)}"), accept,
        )
        try:
            await driver.start()
            await asyncio.wait_for(observed.wait(), timeout=_FIRST_FRAME_TIMEOUT_SECONDS)
            await asyncio.sleep(_EXTRA_FRAME_TIMEOUT_SECONDS)
        finally:
            release.set()
            await driver.stop()
    return SimulatedGatewayIngressResult(
        upstream_authenticated=authenticated,
        positive_forwarded=str(positive["message_id"]) in received,
        negative_dropped=str(negative["message_id"]) not in received,
        positive_frame_sha256=hashlib.sha256(positive_raw.encode()).hexdigest(),
        negative_frame_sha256=hashlib.sha256(negative_raw.encode()).hexdigest(),
    )


__all__ = ["SimulatedGatewayIngressResult", "run_simulated_gateway_ingress"]
