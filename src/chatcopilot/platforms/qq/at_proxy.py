"""OneBot v11 访问代理（QQ 用户/群白名单与群聊 @ 门禁）。

背景：cc-connect 的 NapCat-QQ 适配器在 ``platform/qq/qq.go`` 里把 OneBot 消息的
``at`` 段硬编码丢弃（``case "at": // Ignore``），且 ``allow_from`` 只识别用户号，
不能表达群白名单或可靠判断 @。

本代理插在 ``NapCat 正向 WS`` 与 ``cc-connect`` 之间：

    QQ ⇄ NapCat(:3001) ⇄ [本代理 :3002] ⇄ cc-connect ⇄ ACP server

- 对 cc-connect 暴露一个 WS 服务端；每条 cc-connect 连接对上游 NapCat 开一条 WS。
- ``cc-connect → NapCat`` 方向（API 调用 / echo）原样透传。
- ``NapCat → cc-connect`` 方向按 :func:`should_forward` 过滤：私聊只认用户白名单；
  群聊允许用户白名单或群白名单命中，并按实例策略继续要求 @机器人。
- API 响应、心跳、notice 等非消息帧一律透传。

配置边界 fail-closed：进程启动前强制校验机器人号、强 token 和回环地址。群名单缺失或
为空不会授予群权限；只有显式 ``*`` 才允许所有群。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from chatcopilot.core.logging import configure_logging
from chatcopilot.core.ingress_receipts import (
    IngressReceiptError,
    append_ingress_receipt,
    receipt_root_from_env,
)
from chatcopilot.platforms.qq.gateway_health import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)
from chatcopilot.project import ENV_PREFIX

_LOGGER = logging.getLogger("chatcopilot.platforms.qq.at_proxy")

_DEFAULT_LISTEN = "ws://127.0.0.1:3002"
_DEFAULT_UPSTREAM = "ws://127.0.0.1:3001"


# ---------------------------------------------------------------------------
# 过滤纯函数（可单测，与 IO 解耦）
# ---------------------------------------------------------------------------
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
    """事件的 message 里是否 @ 了本机器人。"""
    message = event.get("message")
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            data = seg.get("data") or {}
            qq = str(data.get("qq", "")).strip()
            if not qq:
                continue
            if qq == bot_qq:
                return True
            if at_all_counts and qq.lower() == "all":
                return True

    # 字符串 / raw_message（CQ 码）兜底
    raw = event.get("raw_message")
    if not isinstance(raw, str) and isinstance(message, str):
        raw = message
    if isinstance(raw, str) and raw:
        if re.search(rf"\[CQ:at,[^\]]*qq={re.escape(bot_qq)}\b", raw, re.IGNORECASE):
            return True
        if at_all_counts and re.search(r"\[CQ:at,[^\]]*qq=all\b", raw, re.IGNORECASE):
            return True
    return False


def _parse_allowlist(
    raw: str | None, *, empty_means_all: bool
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
    """Return the structured proxy decision without exposing allowlist contents.

    - 非 dict / 非 ``message`` 事件（API 响应、meta_event、notice...）→ 透传。
    - 私聊消息：仅用户白名单命中时透传。
    - 群消息：用户或群白名单命中，并满足可选 @ 策略时透传。
    - ``bot_qq`` 为空（配置缺失）→ fail-open 透传 + 由调用方告警。
    """
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
            mention_satisfied=False,
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
    """Compatibility wrapper for the existing boolean filtering contract."""

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
    """Return lossless cc-connect-visible text, excluding only supported @ segments."""

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


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class _ProxyConfig:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        values = os.environ if env is None else env
        self.listen_url = (values.get("QQ_AT_PROXY_URL") or _DEFAULT_LISTEN).strip()
        self.upstream_url = (values.get("QQ_WS_URL") or _DEFAULT_UPSTREAM).strip()
        self.token = (values.get("QQ_ACCESS_TOKEN") or "").strip()
        self.bot_qq = (values.get("QQ_ACCOUNT") or "").strip()
        self.require_at = (
            values.get("QQ_REQUIRE_AT_IN_GROUP") or "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.user_ids, self.allow_all_users = _parse_allowlist(
            values.get("QQ_ALLOW_FROM"), empty_means_all=True
        )
        self.group_ids, self.allow_all_groups = _parse_allowlist(
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
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 3002
        return host, port


def _validate_proxy_config(cfg: "_ProxyConfig") -> None:
    require_loopback_websocket_url(cfg.listen_url, env_key="QQ_AT_PROXY_URL")
    require_loopback_websocket_url(cfg.upstream_url, env_key="QQ_WS_URL")
    require_access_token(cfg.token)
    if not cfg.bot_qq:
        raise QQBoundaryError(
            "qq_account_missing",
            "QQ_ACCOUNT is required when the group mention proxy is enabled",
        )


# ---------------------------------------------------------------------------
# WS 中继
# ---------------------------------------------------------------------------
async def _connect_upstream(cfg: "_ProxyConfig"):
    """连上游 NapCat；兼容 websockets 新旧版本的 header 参数名。"""
    import websockets

    headers = [("Authorization", f"Bearer {cfg.token}")] if cfg.token else None
    if headers is None:
        return await websockets.connect(cfg.upstream_url, max_size=None)
    try:
        return await websockets.connect(
            cfg.upstream_url, additional_headers=headers, max_size=None
        )
    except TypeError:  # websockets < 13 用 extra_headers
        return await websockets.connect(
            cfg.upstream_url, extra_headers=headers, max_size=None
        )


async def _pump_downstream(napcat_ws: Any, cc_ws: Any, cfg: "_ProxyConfig") -> None:
    """NapCat → cc-connect：按 @ 过滤后转发。"""
    async for raw in napcat_ws:
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            await cc_ws.send(raw)  # 非 JSON 原样透传
            continue
        decision = evaluate_forward(
            event,
            cfg.bot_qq,
            cfg.at_all_counts,
            require_at=cfg.require_at,
            user_ids=cfg.user_ids,
            allow_all_users=cfg.allow_all_users,
            group_ids=cfg.group_ids,
            allow_all_groups=cfg.allow_all_groups,
        )
        if decision.forward:
            await cc_ws.send(raw)
            _record_forward_receipt(event, decision, cfg)
        else:
            _LOGGER.info(
                "drop QQ message by access proxy | kind=%s reason=%s",
                decision.chat_kind or "unknown",
                decision.code,
            )


def _record_forward_receipt(
    event: Mapping[str, object],
    decision: ForwardDecision,
    cfg: "_ProxyConfig",
) -> None:
    """Persist optional digest-only evidence after downstream forwarding succeeds."""

    if not decision.message_event or cfg.receipt_root is None:
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
            cfg.receipt_root,
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
    """cc-connect → NapCat：原样透传（API 调用 / echo）。"""
    async for raw in cc_ws:
        await napcat_ws.send(raw)


async def _handle_cc_connection(cc_ws: Any, cfg: "_ProxyConfig") -> None:
    import websockets

    try:
        napcat_ws = await _connect_upstream(cfg)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("upstream connect failed (%s): %s", cfg.upstream_url, exc)
        await cc_ws.close()
        return

    _LOGGER.info("cc-connect attached; upstream %s connected", cfg.upstream_url)
    try:
        async with napcat_ws:
            down = asyncio.create_task(_pump_downstream(napcat_ws, cc_ws, cfg))
            up = asyncio.create_task(_pump_upstream(cc_ws, napcat_ws))
            done, pending = await asyncio.wait(
                {down, up}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except websockets.ConnectionClosed:
        pass
    except Exception:  # noqa: BLE001
        _LOGGER.exception("relay error")
    finally:
        _LOGGER.info("cc-connect detached")


async def _amain(cfg: "_ProxyConfig") -> None:
    import websockets

    host, port = cfg.listen_host_port

    async def handler(cc_ws: Any, *_: Any) -> None:
        await _handle_cc_connection(cc_ws, cfg)

    _LOGGER.info(
        "qq access proxy listening on ws://%s:%d -> upstream %s "
        "(bot_identity_configured=%s, require_at=%s, at_all_counts=%s)",
        host,
        port,
        cfg.upstream_url,
        bool(cfg.bot_qq),
        cfg.require_at,
        cfg.at_all_counts,
    )
    if cfg.receipt_root_error:
        _LOGGER.warning(
            "QQ ingress receipts disabled; forwarding is unchanged | reason=%s",
            cfg.receipt_root_error,
        )
    async with websockets.serve(handler, host, port, max_size=None):
        await asyncio.Future()  # run forever


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO", f"{ENV_PREFIX}_ACP_LOG_LEVEL")
    try:
        import websockets  # noqa: F401
    except ImportError:
        _LOGGER.error(
            "缺少 websockets 依赖，QQ @ 过滤代理无法启动；请把 websockets 装进实例 venv 后重试。"
        )
        return 1
    cfg = _ProxyConfig()
    try:
        _validate_proxy_config(cfg)
    except QQBoundaryError as exc:
        _LOGGER.error("QQ @ proxy boundary rejected | code=%s error=%s", exc.error_code, exc)
        return 2
    try:
        asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = [
    "ForwardDecision",
    "_ProxyConfig",
    "_validate_proxy_config",
    "evaluate_forward",
    "normalized_onebot_text",
    "should_forward",
    "main",
]
