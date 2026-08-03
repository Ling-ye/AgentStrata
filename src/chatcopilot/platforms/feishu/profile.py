"""飞书用户资料查询。

ACP 会话只能稳定拿到 open_id；显示名不一定会随 cc-connect hook 注入。
这里用机器人自身的 app_id/app_secret 向飞书开放平台回查当前发言人的资料。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger("chatcopilot.platforms.feishu.profile")

_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}


@dataclass(frozen=True)
class FeishuUserProfile:
    """飞书用户资料中当前需要的最小字段。"""

    user_id: str
    display_name: Optional[str] = None


class FeishuProfileError(RuntimeError):
    """飞书用户资料查询失败。"""


def _post_json(url: str, payload: Dict[str, Any], *, timeout: int) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    return _request_json(req, timeout=timeout)


def _get_json(url: str, *, token: str, timeout: int) -> Dict[str, Any]:
    req = Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    return _request_json(req, timeout=timeout)


def _request_json(req: Request, *, timeout: int) -> Dict[str, Any]:
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed Feishu API URL
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise FeishuProfileError(f"飞书接口 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FeishuProfileError(f"飞书接口网络错误: {exc.reason}") from exc
    except OSError as exc:
        raise FeishuProfileError(f"飞书接口请求失败: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuProfileError("飞书接口返回非 JSON 内容") from exc
    if not isinstance(data, dict):
        raise FeishuProfileError("飞书接口返回结构异常")
    return data


def _require_success(data: Dict[str, Any], *, action: str) -> None:
    code = data.get("code")
    if code not in (0, "0", None):
        msg = data.get("msg") or data.get("message") or "unknown error"
        raise FeishuProfileError(f"{action}失败: code={code} msg={msg}")


def get_tenant_access_token(
    *,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    timeout: int = 5,
) -> str:
    """获取并缓存 tenant_access_token。"""
    resolved_app_id = (app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
    resolved_secret = (app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not resolved_app_id or not resolved_secret:
        raise FeishuProfileError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")

    cache_key = (resolved_app_id, resolved_secret)
    cached = _TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached is not None:
        token, expires_at = cached
        if token and expires_at > now + 60:
            return token

    data = _post_json(
        _OPEN_API_BASE + _TOKEN_PATH,
        {"app_id": resolved_app_id, "app_secret": resolved_secret},
        timeout=timeout,
    )
    _require_success(data, action="获取 tenant_access_token")
    token = str(data.get("tenant_access_token") or "").strip()
    if not token:
        raise FeishuProfileError("飞书接口未返回 tenant_access_token")

    expire = data.get("expire")
    try:
        ttl = max(60, int(expire))
    except (TypeError, ValueError):
        ttl = 3600
    _TOKEN_CACHE[cache_key] = (token, now + ttl)
    return token


def fetch_user_profile(
    user_id: str,
    *,
    token: Optional[str] = None,
    timeout: int = 5,
) -> FeishuUserProfile:
    """按 open_id 查询飞书用户资料。"""
    normalized_user_id = (user_id or "").strip()
    if not normalized_user_id:
        raise FeishuProfileError("user_id 为空，无法查询飞书用户资料")

    resolved_token = token or get_tenant_access_token(timeout=timeout)
    url = (
        f"{_OPEN_API_BASE}/contact/v3/users/{quote(normalized_user_id, safe='')}"
        "?user_id_type=open_id"
    )
    data = _get_json(url, token=resolved_token, timeout=timeout)
    _require_success(data, action="查询飞书用户资料")

    user = (data.get("data") or {}).get("user") if isinstance(data.get("data"), dict) else None
    if not isinstance(user, dict):
        raise FeishuProfileError("飞书接口未返回用户资料")

    display_name = _pick_display_name(user)
    return FeishuUserProfile(user_id=normalized_user_id, display_name=display_name)


def _pick_display_name(user: Dict[str, Any]) -> Optional[str]:
    for key in ("name", "nickname", "en_name"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_user_name_from_feishu(user_id: Optional[str]) -> Optional[str]:
    """查询显示名的安全入口；失败只记日志并返回 None，避免阻断会话。"""
    normalized_user_id = (user_id or "").strip()
    if not normalized_user_id:
        return None
    try:
        profile = fetch_user_profile(normalized_user_id)
    except FeishuProfileError as exc:
        _LOGGER.warning("resolve user name from Feishu failed: %s", exc)
        return None
    return profile.display_name


__all__ = [
    "FeishuProfileError",
    "FeishuUserProfile",
    "fetch_user_profile",
    "get_tenant_access_token",
    "resolve_user_name_from_feishu",
]
