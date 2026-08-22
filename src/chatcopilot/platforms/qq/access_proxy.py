"""OneBot access filtering and authenticated loopback WebSocket relay."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from chatcopilot.core.ingress_receipts import (
    IngressReceiptError,
    append_ingress_receipt,
    receipt_root_from_env,
)
from chatcopilot.platforms.qq.boundary import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)

_LOGGER = logging.getLogger("chatcopilot.platforms.qq.at_proxy")
_DEFAULT_LISTEN = "ws://127.0.0.1:3002"
_DEFAULT_UPSTREAM = "ws://127.0.0.1:3001"


@dataclass(frozen=True)
class ForwardDecision:
    forward: bool
    code: str
    message_event: bool
    chat_kind: str = ""
    user_allowed: bool = False
    group_allowed: bool = False
    mention_required: bool = False
    mention_satisfied: bool = False

    def receipt_payload(self) -> dict[str, object]:
        return {
            "code": self.code,
            "outcome": "forward" if self.forward else "drop",
            "user_allowed": self.user_allowed,
            "group_allowed": self.group_allowed,
            "mention_required": self.mention_required,
            "mention_satisfied": self.mention_satisfied,
        }


def _has_self_at(event: dict[str, Any], bot_qq: str, at_all_counts: bool) -> bool:
    message = event.get("message")
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            data = segment.get("data") or {}
            qq = str(data.get("qq", "")).strip()
            if qq == bot_qq or (at_all_counts and qq.lower() == "all"):
                return True
    raw = event.get("raw_message")
    if not isinstance(raw, str) and isinstance(message, str):
        raw = message
    if isinstance(raw, str) and raw:
        if re.search(rf"\[CQ:at,[^\]]*qq={re.escape(bot_qq)}\b", raw, re.IGNORECASE):
            return True
        if at_all_counts and re.search(r"\[CQ:at,[^\]]*qq=all\b", raw, re.IGNORECASE):
            return True
    return False


def parse_allowlist(
    raw: str | None,
    *,
    empty_means_all: bool,
) -> tuple[frozenset[str], bool]:
    value = (raw or "").strip()
    if not value:
        return frozenset(), empty_means_all
    items = frozenset(item.strip() for item in value.split(",") if item.strip())
    if "*" in items:
        return frozenset(), True
    return items, False


def evaluate_forward(
    event: Any,
    bot_qq: str,
    at_all_counts: bool = False,
    *,
    require_at: bool = True,
    user_ids: frozenset[str] = frozenset(),
    allow_all_users: bool = True,
    group_ids: frozenset[str] = frozenset(),
    allow_all_groups: bool = False,
) -> ForwardDecision:
    """Return the structured forwarding decision without exposing allowlist values."""

    if not isinstance(event, dict):
        return ForwardDecision(True, "non_object_passthrough", False)
    if event.get("post_type") != "message":
        return ForwardDecision(True, "non_message_passthrough", False)
    message_type = event.get("message_type")
    user_id = str(event.get("user_id") or "").strip()
    user_allowed = allow_all_users or (bool(user_id) and user_id in user_ids)
    if message_type == "private":
        return ForwardDecision(
            user_allowed,
            "private_user_allowed" if user_allowed else "private_user_denied",
            True,
            chat_kind="p2p",
            user_allowed=user_allowed,
            mention_satisfied=True,
        )
    if message_type != "group":
        return ForwardDecision(
            False,
            "unsupported_message_type",
            True,
            chat_kind=str(message_type or "unknown")[:40],
            user_allowed=user_allowed,
        )
    group_id = str(event.get("group_id") or "").strip()
    group_allowed = allow_all_groups or (bool(group_id) and group_id in group_ids)
    if not (user_allowed or group_allowed):
        return ForwardDecision(
            False,
            "group_not_allowed",
            True,
            chat_kind="group",
            user_allowed=user_allowed,
            group_allowed=group_allowed,
            mention_required=require_at,
        )
    if not require_at:
        return ForwardDecision(
            True,
            "group_allowed_without_mention",
            True,
            chat_kind="group",
            user_allowed=user_allowed,
            group_allowed=group_allowed,
            mention_satisfied=True,
        )
    if not bot_qq:
        return ForwardDecision(
            True,
            "bot_identity_missing_compatibility",
            True,
            chat_kind="group",
            user_allowed=user_allowed,
            group_allowed=group_allowed,
            mention_required=True,
        )
    mention_satisfied = _has_self_at(event, bot_qq, at_all_counts)
    return ForwardDecision(
        mention_satisfied,
        "group_mention_matched" if mention_satisfied else "group_mention_missing",
        True,
        chat_kind="group",
        user_allowed=user_allowed,
        group_allowed=group_allowed,
        mention_required=True,
        mention_satisfied=mention_satisfied,
    )


def should_forward(
    event: Any,
    bot_qq: str,
    at_all_counts: bool = False,
    *,
    require_at: bool = True,
    user_ids: frozenset[str] = frozenset(),
    allow_all_users: bool = True,
    group_ids: frozenset[str] = frozenset(),
    allow_all_groups: bool = False,
) -> bool:
    """Compatibility wrapper for the established boolean filter contract."""

    return evaluate_forward(
        event,
        bot_qq,
        at_all_counts,
        require_at=require_at,
        user_ids=user_ids,
        allow_all_users=allow_all_users,
        group_ids=group_ids,
        allow_all_groups=allow_all_groups,
    ).forward


def normalized_onebot_text(event: Mapping[str, object]) -> tuple[str, int] | None:
    """Return lossless cc-connect-visible text, excluding supported @ segments."""

    message = event.get("message")
    if isinstance(message, list):
        pieces: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                return None
            segment_type = segment.get("type")
            data = segment.get("data")
            if not isinstance(data, dict):
                return None
            if segment_type == "at":
                continue
            if segment_type != "text" or not isinstance(data.get("text"), str):
                return None
            pieces.append(str(data["text"]))
        return "".join(pieces).strip(), len(message)
    if isinstance(message, str) and "[CQ:" not in message:
        return message.strip(), 1
    raw = event.get("raw_message")
    if isinstance(raw, str) and "[CQ:" not in raw:
        return raw.strip(), 1
    return None


class ProxyConfig:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        values = os.environ if env is None else env
        self.listen_url = (values.get("QQ_AT_PROXY_URL") or _DEFAULT_LISTEN).strip()
        self.upstream_url = (values.get("QQ_WS_URL") or _DEFAULT_UPSTREAM).strip()
        self.token = (values.get("QQ_ACCESS_TOKEN") or "").strip()
        self.bot_qq = (values.get("QQ_ACCOUNT") or "").strip()
        self.require_at = (values.get("QQ_REQUIRE_AT_IN_GROUP") or "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.user_ids, self.allow_all_users = parse_allowlist(
            values.get("QQ_ALLOW_FROM"), empty_means_all=True
        )
        self.group_ids, self.allow_all_groups = parse_allowlist(
            values.get("QQ_ALLOW_GROUPS"), empty_means_all=False
        )
        self.at_all_counts = (values.get("QQ_AT_ALL_COUNTS") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            self.receipt_root = receipt_root_from_env(values)
            self.receipt_root_error = ""
        except IngressReceiptError as exc:
            self.receipt_root = None
            self.receipt_root_error = str(exc)

    @property
    def listen_host_port(self) -> tuple[str, int]:
        parts = urlsplit(self.listen_url)
        return parts.hostname or "127.0.0.1", parts.port or 3002


def validate_proxy_config(config: ProxyConfig) -> None:
    require_loopback_websocket_url(config.listen_url, env_key="QQ_AT_PROXY_URL")
    require_loopback_websocket_url(config.upstream_url, env_key="QQ_WS_URL")
    require_access_token(config.token)
    if not config.bot_qq:
        raise QQBoundaryError(
            "qq_account_missing",
            "QQ_ACCOUNT is required when the group mention proxy is enabled",
        )


async def _connect_upstream(config: ProxyConfig) -> Any:
    import websockets

    headers = [("Authorization", f"Bearer {config.token}")]
    try:
        return await websockets.connect(
            config.upstream_url, additional_headers=headers, max_size=None
        )
    except TypeError:
        return await websockets.connect(config.upstream_url, extra_headers=headers, max_size=None)


async def _pump_downstream(napcat_ws: Any, cc_ws: Any, config: ProxyConfig) -> None:
    async for raw in napcat_ws:
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            await cc_ws.send(raw)
            continue
        decision = evaluate_forward(
            event,
            config.bot_qq,
            config.at_all_counts,
            require_at=config.require_at,
            user_ids=config.user_ids,
            allow_all_users=config.allow_all_users,
            group_ids=config.group_ids,
            allow_all_groups=config.allow_all_groups,
        )
        if decision.forward:
            await cc_ws.send(raw)
            _record_forward_receipt(event, decision, config)
        else:
            _LOGGER.info(
                "drop QQ message by access proxy | kind=%s reason=%s",
                decision.chat_kind or "unknown",
                decision.code,
            )


def _record_forward_receipt(
    event: Mapping[str, object],
    decision: ForwardDecision,
    config: ProxyConfig,
) -> None:
    """Persist optional digest-only evidence after downstream forwarding succeeds."""

    if not decision.message_event or config.receipt_root is None:
        return
    normalized = normalized_onebot_text(event)
    if normalized is None:
        _LOGGER.info(
            "QQ ingress receipt omitted | kind=%s reason=non_text_or_lossy",
            decision.chat_kind or "unknown",
        )
        return
    content, segment_count = normalized
    user_id = str(event.get("user_id") or "").strip()
    conversation_id = (
        str(event.get("group_id") or "").strip()
        if decision.chat_kind == "group"
        else user_id
    )
    try:
        append_ingress_receipt(
            config.receipt_root,
            platform="qq",
            chat_kind=decision.chat_kind,
            chat_id=conversation_id,
            actor_id=user_id,
            content=content,
            message_id=event.get("message_id"),
            message_kind="text",
            segment_count=segment_count,
            decision=decision.receipt_payload(),
        )
    except IngressReceiptError as exc:
        _LOGGER.warning(
            "QQ ingress receipt unavailable; forwarding is unchanged | reason=%s",
            exc,
        )


async def _pump_upstream(cc_ws: Any, napcat_ws: Any) -> None:
    async for raw in cc_ws:
        await napcat_ws.send(raw)


async def handle_cc_connection(cc_ws: Any, config: ProxyConfig) -> None:
    import websockets

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


async def serve_proxy(config: ProxyConfig) -> None:
    import websockets

    host, port = config.listen_host_port

    async def handler(cc_ws: Any, *_: Any) -> None:
        await handle_cc_connection(cc_ws, config)

    _LOGGER.info(
        "qq access proxy listening on ws://%s:%d -> upstream %s "
        "(bot_identity_configured=%s, require_at=%s, at_all_counts=%s)",
        host,
        port,
        config.upstream_url,
        bool(config.bot_qq),
        config.require_at,
        config.at_all_counts,
    )
    if config.receipt_root_error:
        _LOGGER.warning(
            "QQ ingress receipts disabled; forwarding is unchanged | reason=%s",
            config.receipt_root_error,
        )
    async with websockets.serve(handler, host, port, max_size=None):
        await asyncio.Future()


__all__ = [
    "ForwardDecision",
    "ProxyConfig",
    "evaluate_forward",
    "handle_cc_connection",
    "normalized_onebot_text",
    "parse_allowlist",
    "serve_proxy",
    "should_forward",
    "validate_proxy_config",
]
