"""CLI facade for the QQ OneBot access proxy."""

from __future__ import annotations

import asyncio
import logging

from chatcopilot.core.logging import configure_logging
from chatcopilot.platforms.qq.access_proxy import (
    ForwardDecision,
    ProxyConfig,
    evaluate_forward,
    handle_cc_connection,
    normalized_onebot_text,
    serve_proxy,
    should_forward,
    validate_proxy_config,
)
from chatcopilot.platforms.qq.boundary import QQBoundaryError
from chatcopilot.project import ENV_PREFIX

_LOGGER = logging.getLogger("chatcopilot.platforms.qq.at_proxy")

# These aliases preserve the established import surface for external callers.
_ProxyConfig = ProxyConfig
_validate_proxy_config = validate_proxy_config
_handle_cc_connection = handle_cc_connection
_amain = serve_proxy


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO", f"{ENV_PREFIX}_ACP_LOG_LEVEL")
    try:
        import websockets  # noqa: F401
    except ImportError:
        _LOGGER.error(
            "缺少 websockets 依赖，QQ @ 过滤代理无法启动；请把 websockets 装进实例 venv 后重试。"
        )
        return 1
    config = ProxyConfig()
    try:
        validate_proxy_config(config)
    except QQBoundaryError as exc:
        _LOGGER.error("QQ @ proxy boundary rejected | code=%s error=%s", exc.error_code, exc)
        return 2
    try:
        asyncio.run(serve_proxy(config))
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = [
    "ForwardDecision",
    "_ProxyConfig",
    "_handle_cc_connection",
    "_validate_proxy_config",
    "evaluate_forward",
    "main",
    "normalized_onebot_text",
    "should_forward",
]
