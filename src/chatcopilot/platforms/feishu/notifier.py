"""后台任务完成通知的飞书 OpenAPI 发送通道。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chatcopilot.contracts.workspace import WorkspaceView as Workspace

_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
_SEND_MESSAGE_PATH = "/im/v1/messages"
_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}
_DEFAULT_TIMEOUT_SEC = 10


class FeishuNotifyError(RuntimeError):
    """飞书后台通知发送失败。"""


@dataclass(frozen=True)
class FeishuDeliveryTarget:
    receive_id_type: str
    receive_id: str


@dataclass(frozen=True)
class FeishuSendResult:
    receive_id_type: str
    receive_id: str
    message_id: Optional[str] = None
    raw: Dict[str, Any] | None = None


def resolve_delivery_target(ws: Workspace) -> FeishuDeliveryTarget:
    """根据工作区身份选择飞书消息接收目标。"""

    chat_kind = (ws.chat_kind or "").strip().lower()
    if chat_kind == "group":
        if not ws.chat_id:
            raise FeishuNotifyError("群聊后台通知缺少 chat_id")
        return FeishuDeliveryTarget(receive_id_type="chat_id", receive_id=ws.chat_id)

    if ws.user_id:
        return FeishuDeliveryTarget(receive_id_type="open_id", receive_id=ws.user_id)

    if ws.chat_id:
        return FeishuDeliveryTarget(receive_id_type="chat_id", receive_id=ws.chat_id)

    raise FeishuNotifyError("后台通知缺少可用的飞书接收目标")


def send_text_to_workspace(
    ws: Workspace,
    text: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT_SEC,
) -> FeishuSendResult:
    """直接调用飞书 OpenAPI，把文本通知发送到当前工作区对应的会话。"""

    normalized_text = (text or "").strip()
    if not normalized_text:
        raise FeishuNotifyError("后台通知内容为空")

    target = resolve_delivery_target(ws)
    token = get_tenant_access_token(timeout=timeout)
    data = _post_json(
        f"{_OPEN_API_BASE}{_SEND_MESSAGE_PATH}?{urlencode({'receive_id_type': target.receive_id_type})}",
        {
            "receive_id": target.receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": normalized_text}, ensure_ascii=False),
        },
        token=token,
        timeout=timeout,
    )
    _require_success(data, action="发送飞书后台通知")
    message_id = _pick_message_id(data)
    return FeishuSendResult(
        receive_id_type=target.receive_id_type,
        receive_id=target.receive_id,
        message_id=message_id,
        raw=data,
    )


def get_tenant_access_token(
    *,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT_SEC,
) -> str:
    """获取 tenant_access_token，并按 app_id/app_secret 缓存。"""

    resolved_app_id = (app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
    resolved_secret = (app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not resolved_app_id or not resolved_secret:
        raise FeishuNotifyError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")

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
        raise FeishuNotifyError("飞书接口未返回 tenant_access_token")

    try:
        ttl = max(60, int(data.get("expire")))
    except (TypeError, ValueError):
        ttl = 3600
    _TOKEN_CACHE[cache_key] = (token, now + ttl)
    return token


def _post_json(
    url: str,
    payload: Dict[str, Any],
    *,
    token: Optional[str] = None,
    timeout: int,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=body, headers=headers, method="POST")
    return _request_json(req, timeout=timeout)


def _request_json(req: Request, *, timeout: int) -> Dict[str, Any]:
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed Feishu API URL
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise FeishuNotifyError(f"飞书接口 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FeishuNotifyError(f"飞书接口网络错误: {exc.reason}") from exc
    except OSError as exc:
        raise FeishuNotifyError(f"飞书接口请求失败: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuNotifyError("飞书接口返回非 JSON 内容") from exc
    if not isinstance(data, dict):
        raise FeishuNotifyError("飞书接口返回结构异常")
    return data


def _require_success(data: Dict[str, Any], *, action: str) -> None:
    code = data.get("code")
    if code not in (0, "0", None):
        msg = data.get("msg") or data.get("message") or "unknown error"
        raise FeishuNotifyError(f"{action}失败: code={code}, msg={msg}")


def _pick_message_id(data: Dict[str, Any]) -> Optional[str]:
    body = data.get("data")
    if isinstance(body, dict):
        message_id = body.get("message_id")
        if isinstance(message_id, str) and message_id.strip():
            return message_id.strip()
    return None

