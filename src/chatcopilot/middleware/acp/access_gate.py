"""会话访问门禁：私聊/群聊白名单 + 群聊 @机器人 要求。

平台中立：门禁**策略**来自 ``BotSpec.access``（``bot.yaml``），**名单**来自 env
（``access.whitelist_env`` 指向的变量，值放 ``local.env`` 不进 git）。中间件 ACP
server 在进入任何业务逻辑前调用 :func:`evaluate`；未命中则静默忽略该消息。

判断"是否被 @"委托给平台 adapter 的 ``detect_self_mention``（经 ``platforms.router``），
本模块不感知任何具体平台的 @ 形态。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from chatcopilot.botspec.model import AccessSpec
from chatcopilot.middleware.runtime.workspace.model import normalize_chat_kind
from chatcopilot.platforms import router as _platform_router


@dataclass(frozen=True)
class AccessDecision:
    """门禁裁决结果。``reason`` 仅用于日志排查。"""

    allowed: bool
    reason: str


def _parse_whitelist(
    env_name: str | None, env: Mapping[str, str]
) -> tuple[set[str], bool]:
    """解析白名单 env，返回 ``(名单集合, 是否放行所有人)``。

    env 未配置 / 为空 / 为 ``*`` 时按"放行所有人"处理（白名单维度不拦）。
    """
    if not env_name:
        return set(), True
    raw = (env.get(env_name) or "").strip()
    if not raw or raw == "*":
        return set(), True
    ids = {token.strip() for token in raw.split(",") if token.strip()}
    if not ids or "*" in ids:
        return set(), True
    return ids, False


def evaluate(
    access: AccessSpec,
    *,
    platform_type: str,
    chat_kind: str | None,
    user_id: str | None,
    text: str,
    mention_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> AccessDecision:
    """对单条入站消息做门禁裁决。

    未声明任何约束（``access.enabled`` 为 False）时直接放行，保证未配置 ``access``
    的机器人行为完全不变。
    """
    if not access.enabled:
        return AccessDecision(True, "policy-disabled")

    resolved_env: Mapping[str, str] = env if env is not None else os.environ
    kind = normalize_chat_kind(chat_kind, None) or "p2p"
    allow_ids, allow_all = _parse_whitelist(access.whitelist_env, resolved_env)
    uid = (user_id or "").strip()

    def in_whitelist() -> bool:
        if allow_all:
            return True
        return bool(uid) and uid in allow_ids

    if kind == "group":
        if access.group_require_whitelist and not in_whitelist():
            return AccessDecision(False, f"group-not-in-whitelist uid={uid or '?'}")
        if access.group_require_mention:
            mentioned = _platform_router.detect_self_mention(
                platform_type,
                text,
                env=resolved_env,
                mention_name=mention_name,
            )
            if mentioned is False:
                return AccessDecision(False, "group-not-mentioned")
            if mentioned is None:
                # 无法判定 @（平台缺识别本机器人所需配置，如 QQ_ACCOUNT）：放行并
                # 告警，避免把整群消息全部误杀。
                return AccessDecision(True, "group-mention-undetermined")
        return AccessDecision(True, "group-allowed")

    # p2p / 其它会话类型一律按私聊处理。
    if access.private_require_whitelist and not in_whitelist():
        return AccessDecision(False, f"private-not-in-whitelist uid={uid or '?'}")
    return AccessDecision(True, "private-allowed")


__all__ = ["AccessDecision", "evaluate"]
