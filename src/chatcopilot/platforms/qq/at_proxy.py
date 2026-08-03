"""OneBot v11 @ 过滤代理（QQ 群聊"必须 @机器人 才回"）。

背景：cc-connect 的 NapCat-QQ 适配器在 ``platform/qq/qq.go`` 里把 OneBot 消息的
``at`` 段硬编码丢弃（``case "at": // Ignore``），且群消息只过 ``allow_from`` 白名单、
不判 @；也没有任何配置/hook 能改。结果群里白名单用户不 @机器人 也会触发。

本代理插在 ``NapCat 正向 WS`` 与 ``cc-connect`` 之间：

    QQ ⇄ NapCat(:3001) ⇄ [本代理 :3002] ⇄ cc-connect ⇄ ACP server

- 对 cc-connect 暴露一个 WS 服务端；每条 cc-connect 连接对上游 NapCat 开一条 WS。
- ``cc-connect → NapCat`` 方向（API 调用 / echo）原样透传。
- ``NapCat → cc-connect`` 方向按 :func:`should_forward` 过滤：**只有群消息里带
  ``at`` 且 qq==机器人号 才放行**；私聊、API 响应、心跳、notice 等一律透传。

配置边界 fail-closed：进程启动前强制校验机器人号、强 token 和回环地址。帧级过滤只对
可识别的群消息执行；非 message 事件和 API 响应仍透传。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

from chatcopilot.core.logging import configure_logging
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


def should_forward(event: Any, bot_qq: str, at_all_counts: bool = False) -> bool:
    """是否把这条 NapCat→cc-connect 的帧转发给 cc-connect。

    - 非 dict / 非 ``message`` 事件（API 响应、meta_event、notice...）→ 透传。
    - 私聊消息 → 透传。
    - 群消息：仅当 @ 了本机器人才透传；否则丢弃。
    - ``bot_qq`` 为空（配置缺失）→ fail-open 透传 + 由调用方告警。
    """
    if not isinstance(event, dict):
        return True
    if event.get("post_type") != "message":
        return True
    if event.get("message_type") != "group":
        return True
    if not bot_qq:
        return True
    return _has_self_at(event, bot_qq, at_all_counts)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class _ProxyConfig:
    def __init__(self) -> None:
        self.listen_url = (os.environ.get("QQ_AT_PROXY_URL") or _DEFAULT_LISTEN).strip()
        self.upstream_url = (os.environ.get("QQ_WS_URL") or _DEFAULT_UPSTREAM).strip()
        self.token = (os.environ.get("QQ_ACCESS_TOKEN") or "").strip()
        self.bot_qq = (os.environ.get("QQ_ACCOUNT") or "").strip()
        self.at_all_counts = (os.environ.get("QQ_AT_ALL_COUNTS") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

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
        if should_forward(event, cfg.bot_qq, cfg.at_all_counts):
            await cc_ws.send(raw)
        else:
            _LOGGER.info(
                "drop group msg without self-@ | group=%s user=%s",
                event.get("group_id"),
                event.get("user_id"),
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
        "qq @-proxy listening on ws://%s:%d -> upstream %s (bot_qq=%s, at_all_counts=%s)",
        host,
        port,
        cfg.upstream_url,
        cfg.bot_qq or "?",
        cfg.at_all_counts,
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


__all__ = ["_ProxyConfig", "_validate_proxy_config", "should_forward", "main"]
