"""Hermetic QQ gateway ingress probe.

By default this starts a fake NapCat upstream and the real access-proxy relay
on ephemeral loopback listeners, proving only accepted-frame forwarding and
denied-frame dropping.  A hermetic Evaluation may explicitly own a downstream
observer and continue the forwarded synthetic frame through project-local
layers; neither mode connects to QQ or proves an external QQ end-to-end flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from chatcopilot.platforms.qq.access_proxy import (
    ProxyConfig,
    handle_cc_connection,
    should_forward,
    validate_proxy_config,
)

_LOOPBACK_HOST = "127.0.0.1"
_MAX_PROBE_FRAME_BYTES = 64 * 1024
_FIRST_FRAME_TIMEOUT_SECONDS = 2.0
_EXTRA_FRAME_TIMEOUT_SECONDS = 0.15


@dataclass(frozen=True)
class SimulatedGatewayIngressReceipt:
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


def _preferred_numeric(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if normalized.isdigit() and not normalized.startswith("0"):
        return normalized
    return None


def _synthetic_probe_env(base_cfg: ProxyConfig) -> dict[str, str]:
    synthetic_bot = _random_numeric_id(excluded=frozenset())
    synthetic_user = _random_numeric_id(excluded=frozenset({synthetic_bot}))
    synthetic_group = _random_numeric_id(excluded=frozenset({synthetic_bot, synthetic_user}))
    if base_cfg.allow_all_users:
        user_allowlist = "*"
    elif base_cfg.user_ids:
        user_allowlist = synthetic_user
    else:
        raise RuntimeError("gateway policy has no user allowlist shape")
    if base_cfg.allow_all_groups:
        group_allowlist = "*"
    elif base_cfg.group_ids:
        group_allowlist = synthetic_group
    else:
        group_allowlist = ""
    values = {
        "QQ_ACCESS_TOKEN": secrets.token_urlsafe(32),
        "QQ_ACCOUNT": synthetic_bot,
        "QQ_ALLOW_FROM": user_allowlist,
        "QQ_ALLOW_GROUPS": group_allowlist,
        "QQ_REQUIRE_AT_IN_GROUP": "true" if base_cfg.require_at else "false",
        "QQ_AT_ALL_COUNTS": "true" if base_cfg.at_all_counts else "false",
        "QQ_WS_URL": "ws://127.0.0.1:1",
        "QQ_AT_PROXY_URL": "ws://127.0.0.1:1",
        "CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID": synthetic_group,
    }
    if base_cfg.receipt_root is not None:
        values["CHATCOPILOT_INGRESS_RECEIPT_DIR"] = str(base_cfg.receipt_root)
    return values


def _probe_events(
    cfg: ProxyConfig,
    env: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    excluded = frozenset(
        {
            cfg.bot_qq,
            *cfg.user_ids,
            *cfg.group_ids,
        }
    )
    configured_group = _preferred_numeric(env.get("CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID"))
    group_id = configured_group or next(iter(sorted(cfg.group_ids)), None)
    if group_id is None:
        group_id = _random_numeric_id(excluded=excluded)

    if cfg.allow_all_users:
        user_id = _random_numeric_id(excluded=excluded | {group_id})
    elif cfg.user_ids:
        user_id = next(iter(sorted(cfg.user_ids)))
    elif cfg.allow_all_groups or group_id in cfg.group_ids:
        user_id = _random_numeric_id(excluded=excluded | {group_id})
    else:
        raise RuntimeError("gateway policy has no synthetic accepted identity")

    nonce = secrets.token_hex(8)
    positive: dict[str, Any] = {
        "time": 0,
        "self_id": int(cfg.bot_qq),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": secrets.randbelow(2_000_000_000) + 1,
        "group_id": int(group_id),
        "user_id": int(user_id),
        "message": [
            {"type": "at", "data": {"qq": cfg.bot_qq}},
            {"type": "text", "data": {"text": f" ingress-probe-{nonce}"}},
        ],
        "raw_message": f"[CQ:at,qq={cfg.bot_qq}] ingress-probe-{nonce}",
    }
    negative = dict(positive)
    negative["message_id"] = secrets.randbelow(2_000_000_000) + 1
    if cfg.require_at:
        negative["message"] = [
            {"type": "text", "data": {"text": f"ingress-deny-{nonce}"}},
        ]
        negative["raw_message"] = f"ingress-deny-{nonce}"
    else:
        negative["message_type"] = "unsupported"

    if not should_forward(
        positive,
        cfg.bot_qq,
        cfg.at_all_counts,
        require_at=cfg.require_at,
        user_ids=cfg.user_ids,
        allow_all_users=cfg.allow_all_users,
        group_ids=cfg.group_ids,
        allow_all_groups=cfg.allow_all_groups,
    ):
        raise RuntimeError("gateway policy rejected the synthetic positive event")
    if should_forward(
        negative,
        cfg.bot_qq,
        cfg.at_all_counts,
        require_at=cfg.require_at,
        user_ids=cfg.user_ids,
        allow_all_users=cfg.allow_all_users,
        group_ids=cfg.group_ids,
        allow_all_groups=cfg.allow_all_groups,
    ):
        raise RuntimeError("gateway policy accepted the synthetic negative event")
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


async def _connect_loopback(websockets: Any, url: str) -> Any:
    options = {
        "compression": None,
        "max_size": _MAX_PROBE_FRAME_BYTES,
        "open_timeout": 2,
        "close_timeout": 1,
    }
    try:
        return await websockets.connect(url, proxy=None, **options)
    except TypeError:
        return await websockets.connect(url, **options)


async def run_simulated_gateway_ingress(
    env: Mapping[str, str],
    *,
    downstream_observer: Callable[[Mapping[str, Any]], Awaitable[None] | None] | None = None,
) -> SimulatedGatewayIngressReceipt:
    """Run the relay locally and optionally hand its accepted frame to the caller."""

    import websockets

    base_cfg = ProxyConfig(env)
    validate_proxy_config(base_cfg)
    synthetic_env = _synthetic_probe_env(base_cfg)
    synthetic_cfg = ProxyConfig(synthetic_env)
    positive, negative = _probe_events(synthetic_cfg, synthetic_env)
    positive_raw = json.dumps(positive, ensure_ascii=True, separators=(",", ":"))
    negative_raw = json.dumps(negative, ensure_ascii=True, separators=(",", ":"))
    release_upstream = asyncio.Event()
    state = {"upstream_authenticated": False}

    async def fake_napcat(connection: Any, *_: Any) -> None:
        expected = f"Bearer {synthetic_cfg.token}"
        actual = _authorization_header(connection)
        state["upstream_authenticated"] = hmac.compare_digest(actual, expected)
        if not state["upstream_authenticated"]:
            await connection.close(code=1008, reason="authentication required")
            return
        await connection.send(negative_raw)
        await connection.send(positive_raw)
        await release_upstream.wait()

    received: list[str] = []
    try:
        async with websockets.serve(
            fake_napcat,
            _LOOPBACK_HOST,
            0,
            compression=None,
            max_size=_MAX_PROBE_FRAME_BYTES,
        ) as upstream_server:
            upstream_url = f"ws://{_LOOPBACK_HOST}:{_server_port(upstream_server)}"
            probe_env = dict(synthetic_env)
            probe_env["QQ_WS_URL"] = upstream_url
            cfg = ProxyConfig(probe_env)
            validate_proxy_config(cfg)

            async def proxy_handler(connection: Any, *_: Any) -> None:
                await handle_cc_connection(connection, cfg)

            async with websockets.serve(
                proxy_handler,
                _LOOPBACK_HOST,
                0,
                compression=None,
                max_size=_MAX_PROBE_FRAME_BYTES,
            ) as proxy_server:
                proxy_url = f"ws://{_LOOPBACK_HOST}:{_server_port(proxy_server)}"
                downstream = await _connect_loopback(websockets, proxy_url)
                try:
                    try:
                        first = await asyncio.wait_for(
                            downstream.recv(),
                            timeout=_FIRST_FRAME_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        first = None
                    if isinstance(first, bytes):
                        first = first.decode("utf-8", errors="strict")
                    if isinstance(first, str):
                        received.append(first)
                    try:
                        extra = await asyncio.wait_for(
                            downstream.recv(),
                            timeout=_EXTRA_FRAME_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        extra = None
                    if isinstance(extra, bytes):
                        extra = extra.decode("utf-8", errors="strict")
                    if isinstance(extra, str):
                        received.append(extra)
                finally:
                    release_upstream.set()
                    await downstream.close()
    finally:
        release_upstream.set()

    positive_forwarded = positive_raw in received
    if positive_forwarded and downstream_observer is not None:
        observed = downstream_observer(dict(positive))
        if observed is not None:
            await observed

    return SimulatedGatewayIngressReceipt(
        upstream_authenticated=state["upstream_authenticated"],
        positive_forwarded=positive_forwarded,
        negative_dropped=negative_raw not in received,
        positive_frame_sha256=hashlib.sha256(positive_raw.encode("utf-8")).hexdigest(),
        negative_frame_sha256=hashlib.sha256(negative_raw.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "SimulatedGatewayIngressReceipt",
    "run_simulated_gateway_ingress",
]
